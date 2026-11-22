#!/usr/bin/env python3
"""Generate a Google Scholar profile card as an SVG."""

from __future__ import annotations

import argparse
import json
import re
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from typing import Dict, Iterator, List, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


@dataclass
class Publication:
    title: str
    authors: str = ""
    venue: str = ""
    year: str = ""
    citations: int = 0


@dataclass
class ScholarProfile:
    user_id: str
    name: str
    affiliation: str
    fields: List[str]
    source: str = "Google Scholar"
    citations: str = "—"
    h_index: str = "—"
    i10_index: str = "—"
    publications: List[Publication] = field(default_factory=list)


@dataclass
class HTMLNode:
    """Small DOM-like node used to inspect the few HTML fields we need."""

    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List[Union["HTMLNode", str]] = field(default_factory=list)

    def descendants(self) -> Iterator["HTMLNode"]:
        yield self
        for child in self.children:
            if isinstance(child, HTMLNode):
                yield from child.descendants()

    def text_content(self) -> str:
        parts: List[str] = []
        for child in self.children:
            if isinstance(child, HTMLNode):
                parts.append(child.text_content())
            else:
                parts.append(child)
        return " ".join(" ".join(parts).split())

    def find_first(
        self,
        *,
        tag: Optional[str] = None,
        node_id: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> Optional["HTMLNode"]:
        for node in self.descendants():
            if tag is not None and node.tag != tag:
                continue
            if node_id is not None and node.attrs.get("id") != node_id:
                continue
            if class_name is not None and class_name not in node.attrs.get("class", "").split():
                continue
            return node
        return None

    def find_all(
        self,
        *,
        tag: Optional[str] = None,
        node_id: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> List["HTMLNode"]:
        found: List[HTMLNode] = []
        for node in self.descendants():
            if tag is not None and node.tag != tag:
                continue
            if node_id is not None and node.attrs.get("id") != node_id:
                continue
            if class_name is not None and class_name not in node.attrs.get("class", "").split():
                continue
            found.append(node)
        return found

    def direct_children(self, tag: str) -> List["HTMLNode"]:
        return [
            child for child in self.children
            if isinstance(child, HTMLNode) and child.tag == tag
        ]


class _ProfileHTMLParser(HTMLParser):
    """Build a lightweight tree with Python's built-in HTMLParser."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HTMLNode("#document")
        self.stack: List[HTMLNode] = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        node = HTMLNode(
            tag=tag.lower(),
            attrs={key.lower(): (value or "") for key, value in attrs},
        )
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        node = HTMLNode(
            tag=tag.lower(),
            attrs={key.lower(): (value or "") for key, value in attrs},
        )
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # Recover gracefully from imperfectly nested real-world HTML.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(html: str) -> HTMLNode:
    """Parse HTML into a small tree without third-party dependencies."""
    parser = _ProfileHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.root


def parse_profile_id(value: str) -> str:
    """Accept either a Scholar profile URL or the `user` ID itself."""
    value = value.strip()
    if not value:
        raise ValueError("Provide a Google Scholar profile URL or user ID.")

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if "scholar.google." not in parsed.netloc:
            raise ValueError("The URL does not appear to be a Google Scholar URL.")
        user_id = parse_qs(parsed.query).get("user", [None])[0]
        if not user_id:
            raise ValueError("Could not find '?user=…' in the profile URL.")
        return user_id

    return value


def clean_text(node: Optional[HTMLNode]) -> str:
    return node.text_content() if node else ""


def parse_integer(value: str) -> int:
    match = re.search(r"\d[\d,]*", value or "")
    return int(match.group(0).replace(",", "")) if match else 0


def parse_profile_html(user_id: str, html: str) -> ScholarProfile:
    """Extract a profile from Scholar HTML; split out for easy local testing."""
    root = parse_html(html)
    page_text = clean_text(root).lower()
    if "unusual traffic" in page_text or "not a robot" in page_text or "captcha" in page_text:
        raise RuntimeError(
            "Google Scholar returned an anti-bot/CAPTCHA page. Try again later."
        )

    name_node = root.find_first(node_id="gsc_prf_in")
    if name_node is None:
        raise RuntimeError(
            "The profile details were not found. Check that the profile is public, "
            "that the user ID is correct, and that Google Scholar has not changed its page layout."
        )

    name = clean_text(name_node) or "Google Scholar profile"
    affiliation = clean_text(root.find_first(class_name="gsc_prf_il")) or "Research profile"

    fields: list[str] = []
    fields_root = root.find_first(node_id="gsc_prf_int")
    if fields_root: 
        for row in fields_root.direct_children("a"):
            fields.append(clean_text(row))

    stats: Dict[str, str] = {}
    stats_table = root.find_first(node_id="gsc_rsb_st")
    if stats_table:
        for row in stats_table.find_all(tag="tr"):
            cells = row.direct_children("td")
            if len(cells) >= 2:
                label = clean_text(cells[0]).lower()
                all_time_value = clean_text(cells[1])
                stats[label] = all_time_value

    publications: List[Publication] = []
    publication_body = root.find_first(node_id="gsc_a_b")
    rows = publication_body.find_all(class_name="gsc_a_tr") if publication_body else []
    for row in rows:
        title = clean_text(row.find_first(class_name="gsc_a_at"))
        if not title:
            continue

        gray_lines = [clean_text(node) for node in row.find_all(class_name="gs_gray")]
        cited_text = clean_text(row.find_first(class_name="gsc_a_ac"))
        year = clean_text(row.find_first(class_name="gsc_a_y"))

        publications.append(
            Publication(
                title=title,
                authors=gray_lines[0] if gray_lines else "",
                venue=gray_lines[1] if len(gray_lines) > 1 else "",
                year=year,
                citations=parse_integer(cited_text),
            )
        )

    return ScholarProfile(
        user_id=user_id,
        name=name,
        affiliation=affiliation,
        fields=fields,
        citations=stats.get("citations", "—"),
        h_index=stats.get("h-index", "—"),
        i10_index=stats.get("i10-index", "—"),
        publications=publications,
    )


def fetch_profile(user_id: str, timeout: int = 20) -> ScholarProfile:
    """Fetch and parse a public Google Scholar profile using urllib."""
    query = urlencode({"user": user_id, "hl": "en", "cstart": 0, "pagesize": 100})
    url = f"https://scholar.google.com/citations?{query}"

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ResearchProject/1.0; contact: 90468339+FlorianFuerrutter@users.noreply.github.com",
        "Accept-Language": "en-US,en;q=0.9",
    }
    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Google Scholar returned HTTP {exc.code}: {exc.reason}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not retrieve Google Scholar profile: {exc}") from exc

    return parse_profile_html(user_id, html)


def fetch_openalex_profile(
    author_id: str, timeout: int = 20, max_papers: int = 8
) -> ScholarProfile:
    """Fetch an author and their most-cited works from the OpenAlex API."""
    author_id = author_id.strip().rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"A\d+", author_id):
        raise ValueError("OpenAlex author ID must be in the form A1234567890.")

    headers = {
        "User-Agent": (
            "ResearchProject/1.0 "
            "(mailto:90468339+FlorianFuerrutter@users.noreply.github.com)"
        )
    }

    def fetch_json(url: str) -> Dict[str, object]:
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"OpenAlex returned HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not retrieve OpenAlex data: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenAlex returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("OpenAlex returned an unexpected JSON response.")
        return data

    author_url = f"https://api.openalex.org/authors/{author_id}"
    author = fetch_json(author_url)
    name = author.get("display_name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("OpenAlex author response did not include a display name.")

    stats = author.get("summary_stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    institutions = author.get("last_known_institutions") or []
    affiliation = ", ".join(
        institution["display_name"]
        for institution in institutions
        if isinstance(institution, dict) and institution.get("display_name")
    ) or "Research profile"
    topics = author.get("topics") or []
    fields = [
        # topic["display_name"]
        # for topic in topics[:3]
        # if isinstance(topic, dict) and topic.get("display_name")
    ]

    works_query = urlencode({
        "filter": f"author.id:{author_id}",
        "sort": "cited_by_count:desc",
        "per-page": min(max_papers, 200),
        "select": "title,authorships,primary_location,publication_year,cited_by_count",
    })
    works_data = fetch_json(f"https://api.openalex.org/works?{works_query}")
    works = works_data.get("results")
    if not isinstance(works, list):
        raise RuntimeError("OpenAlex works response did not include a results list.")

    publications: List[Publication] = []
    for work in works:
        if not isinstance(work, dict) or not work.get("title"):
            continue
        authorships = work.get("authorships") or []
        authors = ", ".join(
            authorship["author"]["display_name"]
            for authorship in authorships
            if isinstance(authorship, dict)
            and isinstance(authorship.get("author"), dict)
            and authorship["author"].get("display_name")
        )
        location = work.get("primary_location") or {}
        venue = ""
        if isinstance(location, dict):
            source = location.get("source") or {}
            if isinstance(source, dict):
                venue = source.get("display_name") or ""
        publications.append(
            Publication(
                title=work["title"],
                authors=authors,
                venue=venue,
                year=str(work["publication_year"]) if work.get("publication_year") else "",
                citations=work.get("cited_by_count") or 0,
            )
        )

    return ScholarProfile(
        user_id=author_id,
        name=name,
        affiliation=affiliation,
        fields=fields,
        source="OpenAlex",
        citations=str(author.get("cited_by_count", "—")),
        h_index=str(stats.get("h_index", "—")),
        i10_index=str(stats.get("i10_index", "—")),
        publications=publications,
    )


def svg_text(
    x: int,
    y: int,
    value: str,
    size: int | float,
    color: str,
    weight: int = 400,
    anchor: str = "start",
    letter_spacing: Optional[float] = None,
) -> str:
    spacing = f' letter-spacing="{letter_spacing}"' if letter_spacing is not None else ""
    return (
        f'<text x="{x}" y="{y}" fill="{color}" font-family="Inter, Segoe UI, Arial, sans-serif" '
        f'font-size="{int(size)}" font-weight="{weight}" text-anchor="{anchor}"{spacing}>'
        f"{escape(value)}</text>"
    )


def shorten(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def make_svg(profile: ScholarProfile, max_papers: int = 8, theme: str = "dark") -> str:
    papers = (profile.publications or [])[:max_papers]

    # Taller publication rows give the title, authors, and venue their own lines.
    row_height = 96
    row_gap = 12
    row_step = row_height + row_gap
    row_start = 388
    row_count = max(1, len(papers))
    # Keep a consistent breathing space between the final row and the footer.
    height = row_start + row_count * row_height + (row_count - 1) * row_gap + 50 #112

    if theme == "light":
        c = {
            "bg": "#F3F6FB", "panel": "#FFFFFF", "panel2": "#F7F9FC",
            "border": "#E2E8F0", "text": "#172033", "muted": "#68758A",
            "accent": "#2563EB", "accent_bg": "#EAF1FF", "green": "#12805C",
        }
    else:
        c = {
            "bg": "#0B1220", "panel": "#111C2E", "panel2": "#152238",
            "border": "#26364D", "text": "#F1F5F9", "muted": "#9AAAC0",
            "accent": "#67E8F9", "accent_bg": "#123344", "green": "#6EE7B7",
        }

    dy = -40
    t_scale = 1.45

    research_fields = " · ".join(x for x in profile.fields if x)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" viewBox="0 0 1100 {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(profile.source)} profile: {escape(profile.name)}</title>',
        f'<desc id="desc">A summary of {escape(profile.source)} citation metrics and selected publications.</desc>',
        f'<rect width="1100" height="{height}" rx="28" fill="{c["bg"]}"/>',
        f'<rect x="1" y="1" width="1098" height="{height - 2}" rx="27" fill="none" stroke="{c["border"]}"/>',
        svg_text(56, 119+dy, shorten(profile.name, 52), 34*t_scale, c["text"], 700),
        svg_text(57, 161+dy, shorten("PhD Student, " + profile.affiliation, 105), 15*t_scale, c["accent"], 400),
        # Research fields are anchored to the right edge for a clean header alignment.
        svg_text(1044, 151+dy, shorten(research_fields, 76), 15, c["muted"], 400, "end"),
    ]

    dy = -30

    metric_data = [
        ("TOTAL CITATIONS", profile.citations),
        ("H-INDEX", profile.h_index),
        ("i10-INDEX", profile.i10_index),
    ]
    card_y, card_h, card_w, gap = 184, 94, 316, 20
    for i, (label, value) in enumerate(metric_data):
        x = 56 + i * (card_w + gap)
        out.extend([
            f'<rect x="{x}" y="{card_y+dy}" width="{card_w}" height="{card_h}" rx="16" fill="{c["panel"]}" stroke="{c["border"]}"/>',
            svg_text(x + 20, card_y + 30+dy, label, 11*t_scale*0.9, c["muted"], 700, letter_spacing=1.2),
            svg_text(x + 20, card_y + 72+dy, value, 29*t_scale*0.9, c["text"], 700),
        ])

    section_y = 329 + dy
    out.extend([
        f'<path d="M56 {section_y}H1044" stroke="{c["border"]}"/>',
        svg_text(56, section_y + 39, "SELECTED PUBLICATIONS", 14, c["text"], 700, letter_spacing=1.3),
    ])

    if papers:
        for i, paper in enumerate(papers):
            y = row_start + i * row_step + dy
            publication_info = (paper.venue or "").strip()
            if paper.year and publication_info:
                # The year already has its own right-aligned column; avoid repeating it.
                publication_info = re.sub(
                    rf"[\s,;]+{re.escape(paper.year)}\s*$", "", publication_info
                ).strip(" ,;")
            out.extend([
                f'<rect x="56" y="{y}" width="988" height="{row_height}" rx="13" fill="{c["panel2"]}" stroke="{c["border"]}"/>',
                svg_text(77, y + 38, f"{i + 1:02d}", 12, c["accent"], 700),
                # Three distinct text lines: publication title, author list, and venue/details.
                svg_text(119, y + 29, shorten(paper.title, 84), 16, c["text"], 650),
                svg_text(119, y + 54, shorten(paper.authors, 1800), 11, c["muted"], 400),
                svg_text(119, y + 77, shorten(publication_info, 120), 11, c["muted"], 400),
                # Consistent right-aligned citation and year columns.
                svg_text(944, y + 42, f"{paper.citations:,}", 17, c["green"], 700, "end"),
                svg_text(944, y + 65, "CITATIONS", 9, c["muted"], 600, "end", 0.8),
                svg_text(1020, y + 42, paper.year or "—", 15, c["text"], 650, "end"),
                svg_text(1020, y + 65, "YEAR", 9, c["muted"], 600, "end", 0.8),
            ])
    else:
        y = row_start
        out.extend([
            f'<rect x="56" y="{y}" width="988" height="{row_height}" rx="13" fill="{c["panel2"]}" stroke="{c["border"]}"/>',
            svg_text(78, y + 54, "No publications could be read from this profile page.", 14, c["muted"], 400),
        ])

    dy = -0
    footer_y = height - 30 + dy
    out.extend([
        svg_text(56, footer_y, f"DATA SOURCE  ·  {profile.source.upper()} PROFILE", 10, c["muted"], 600, letter_spacing=0.9),
        svg_text(1044, footer_y, f"UPDATED {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC", 10, c["muted"], 600, "end", 0.8),
        "</svg>",
    ])
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a modern GitHub README SVG from a public research profile."
    )
    parser.add_argument(
        "--profile",
        default="c7ALphMAAAAJ",
        help="Google Scholar profile URL or the value of its 'user' parameter (primary source)",
    )
    parser.add_argument(
        "--openalex-author",
        default="A5093211931",
        help="OpenAlex author ID or URL used if Google Scholar is unavailable",
    )
    parser.add_argument("-o", "--output", default="out/profile.svg", help="Output SVG filename (default: profile.svg)")
    parser.add_argument("--max-papers", type=int, default=8, help="Maximum publications shown (default: 8)")
    parser.add_argument("--theme", choices=["dark", "light"], default="light", help="SVG color theme")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP request timeout in seconds")
    args = parser.parse_args()

    if args.max_papers < 1:
        parser.error("--max-papers must be at least 1")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1 second")

    try:
        user_id = parse_profile_id(args.profile)
        try:
            raise RuntimeError()
            #profile = fetch_profile(user_id, timeout=args.timeout)

        except RuntimeError as scholar_error:
            print(
                f"Google Scholar unavailable ({scholar_error}); trying OpenAlex.",
                file=sys.stderr,
            )
            try:
                profile = fetch_openalex_profile(
                    args.openalex_author,
                    timeout=args.timeout,
                    max_papers=args.max_papers,
                )
            except (ValueError, RuntimeError) as openalex_error:
                raise RuntimeError(
                    f"Google Scholar failed: {scholar_error}; "
                    f"OpenAlex fallback failed: {openalex_error}"
                ) from openalex_error
        svg = make_svg(profile, max_papers=args.max_papers, theme=args.theme)
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(svg)
        print(f"Created {args.output} for {profile.name}.")
        print(
            f"Metrics: citations={profile.citations}, "
            f"h-index={profile.h_index}, i10-index={profile.i10_index}"
        )
        print(f"Publications parsed: {len(profile.publications)}")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
