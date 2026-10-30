#!/usr/bin/env python3
"""Source-aware arXiv **listing** fetch + parse for the evolution research stage (#299).

WHY
---
The research stage used to read arXiv ``/list/<cat>/new`` pages through the
generic web extractor. Measured in this pipeline's own instrument accounting
(``research/2026-10-26.md`` §2.1 / §2.3): the extractor returned ~200 bytes and
**no paper ids** for a listing page, while a direct fetch of the same nine
categories returned 87 KB in 0.6 s with 1,045 parseable records — ids and
abstracts included. Because the abstracts did not survive, the cycle report had
to declare "abstracts only" for two consecutive cycles and then make a second
instrument (the arXiv API) recover facts that were already on the page.

WHAT
----
``parse_listing`` reads the listing HTML *as arXiv writes it*: one
``<dl id='articles'>`` block per announcement section (``New submissions`` /
``Cross submissions`` / ``Replacements``), each with a ``(showing X of Y
entries)`` header, and inside it one ``<dt>``/``<dd>`` pair per paper carrying
the arXiv id, title, authors, subjects, comments and abstract. Every record is
therefore id-bearing and abstract-bearing by construction.

Reconciliation is asserted in-stage: the parsed entry count per section must
equal that section's own declared total, the per-section totals must sum to the
page's ``Total of N entries`` line, and every parsed record must carry an id and
an abstract. A mismatch is a hard, loud failure — the stage never proceeds on a
silent partial parse (that failure mode is exactly how the HTML structure was
mis-read twice, caught only by ad-hoc reconciliation against printed counts).

FETCH
-----
``fetch_listing`` uses ``urllib`` with a browser User-Agent and an explicit
timeout, so no shell/``curl`` dependency and no unbounded hang. Offline use is
supported (and is what the tests exercise) via ``--from-html``.

WIRING
------
This is not a standalone utility: ``scripts/evolution_research_web_gate.sh``
runs it as part of the research job's pre-agent preflight (the same gate that
already probes ``https://arxiv.org``), writing
``$RESEARCH_DIR/raw/<date>.jsonl``. The ``evolution-research`` skill reads that
file instead of extracting the listing with the generic web extractor. When the
probe cannot run (egress-restricted install, parse mismatch) the gate leaves no
JSONL and drops a reason sidecar, and the skill falls back to the documented
extractor path. The extractor is kept as fallback, never deleted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_CATEGORIES: tuple[str, ...] = ("cs.AI", "cs.LG", "cs.CL")
LISTING_URL = "https://arxiv.org/list/{category}/new?skip=0&show=2000"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 hermes-evolution-research/1.0"
)
FETCH_TIMEOUT = 30.0

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# One announcement block per section; arXiv emits several <dl id='articles'>.
_SECTION_RE = re.compile(
    r"<dl id=['\"]articles['\"]>(?P<body>.*?)</dl>", re.DOTALL | re.IGNORECASE
)
_HEADER_RE = re.compile(
    r"<h3>(?P<label>[^<(]+?)\s*\(showing\s+(?P<shown>\d+)\s+of\s+"
    r"(?P<total>\d+)\s+entries\)</h3>",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(
    r"<dt>(?P<dt>.*?)</dt>\s*<dd>(?P<dd>.*?)</dd>", re.DOTALL | re.IGNORECASE
)
_ID_RE = re.compile(
    r"href\s*=\s*['\"]/abs/(?P<href>[^'\"]+)['\"][^>]*?title=['\"]Abstract['\"]",
    re.IGNORECASE,
)
_BARE_ID_RE = re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?:v(?P<version>\d+))?$")
_TITLE_RE = re.compile(
    r"<div class=['\"]list-title[^'\"]*['\"]>(?P<body>.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
_AUTHORS_RE = re.compile(
    r"<div class=['\"]list-authors['\"]>(?P<body>.*?)</div>", re.DOTALL | re.IGNORECASE
)
_COMMENTS_RE = re.compile(
    r"<div class=['\"]list-comments[^'\"]*['\"]>(?P<body>.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
_SUBJECTS_RE = re.compile(
    r"<div class=['\"]list-subjects[^'\"]*['\"]>(?P<body>.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
_ABSTRACT_RE = re.compile(
    r"<p class=['\"]mathjax['\"]>(?P<body>.*?)</p>", re.DOTALL | re.IGNORECASE
)
_PRIMARY_RE = re.compile(
    r"<span class=['\"]primary-subject['\"]>(?P<body>.*?)</span>",
    re.DOTALL | re.IGNORECASE,
)
_AUTHOR_LINK_RE = re.compile(r"<a\b[^>]*>(?P<name>.*?)</a>", re.DOTALL | re.IGNORECASE)
_CATEGORY_CODE_RE = re.compile(r"\(([a-z][a-z\-]*(?:\.[A-Za-z\-]+)?)\)")
_DESCRIPTOR_RE = re.compile(r"<span class=['\"]descriptor['\"]>.*?</span>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_LISTING_DATE_RE = re.compile(
    r"Showing new listings for\s+(?:[A-Za-z]+,\s*)?(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})"
)
_TOTAL_RE = re.compile(r"Total of\s+(?P<total>\d+)\s+entries")


def _text(fragment: str) -> str:
    """Strip tags/descriptors, unescape entities and collapse whitespace."""
    fragment = _DESCRIPTOR_RE.sub(" ", fragment)
    return " ".join(unescape(_TAG_RE.sub(" ", fragment)).split())


def _announced_date(html: str) -> str | None:
    """The announcement date printed in the page header (ISO 8601), if present."""
    match = _LISTING_DATE_RE.search(html)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    try:
        return date(
            int(match.group("year")), month, int(match.group("day"))
        ).isoformat()
    except ValueError:
        return None


@dataclass
class Section:
    """One announcement block and how much of it the parser recovered."""

    label: str
    declared: int
    parsed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "declared": self.declared, "parsed": self.parsed}


@dataclass
class ListingPage:
    """Parsed listing page for one category."""

    category: str
    url: str = ""
    announced: str | None = None
    declared_total: int | None = None
    sections: list[Section] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        """Reconciliation failures; empty means the parse is trustworthy."""
        problems: list[str] = []
        for section in self.sections:
            if section.parsed != section.declared:
                problems.append(
                    f"{self.category} {section.label}: parsed {section.parsed} "
                    f"entries, page declares {section.declared}"
                )
        if self.sections and self.declared_total is not None:
            declared_sum = sum(section.declared for section in self.sections)
            if declared_sum != self.declared_total:
                problems.append(
                    f"{self.category}: section totals sum to {declared_sum}, "
                    f"page declares 'Total of {self.declared_total} entries'"
                )
        if not self.records:
            problems.append(f"{self.category}: no listing records parsed")
        for record in self.records:
            if not record.get("id"):
                problems.append(f"{self.category}: record without an id")
            elif not record.get("title"):
                problems.append(
                    f"{self.category} {record['id']}: record without a title"
                )
            elif not record.get("abstract"):
                problems.append(
                    f"{self.category} {record['id']}: record without an abstract"
                )
        return problems


def parse_listing(html: str, category: str, url: str = "") -> ListingPage:
    """Parse an arXiv listing page into id-bearing, abstract-bearing records."""
    page = ListingPage(
        category=category, url=url or LISTING_URL.format(category=category)
    )
    page.announced = _announced_date(html)
    total_match = _TOTAL_RE.search(html)
    page.declared_total = int(total_match.group("total")) if total_match else None

    seen: set[tuple[str, str]] = set()
    for block in _SECTION_RE.finditer(html):
        body = block.group("body")
        header = _HEADER_RE.search(body)
        if not header:
            continue
        section = Section(
            label=" ".join(header.group("label").split()),
            declared=int(header.group("total")),
        )
        entries = list(_ENTRY_RE.finditer(body))
        section.parsed = len(entries)
        page.sections.append(section)

        for entry in entries:
            record = _parse_entry(
                entry.group("dt"),
                entry.group("dd"),
                category=category,
                section=section.label,
                announced=page.announced,
                url=page.url,
            )
            key = (str(record.get("id") or ""), section.label)
            if key in seen:
                continue
            seen.add(key)
            page.records.append(record)
    return page


def _first(pattern: re.Pattern[str], html: str) -> str:
    """Text of the first ``pattern`` match in ``html`` (empty when absent)."""
    match = pattern.search(html)
    return _text(match.group("body")) if match else ""


def _authors(dd_html: str) -> str:
    """Author names joined with ``, `` (arXiv wraps each in its own link)."""
    match = _AUTHORS_RE.search(dd_html)
    if not match:
        return ""
    names = [
        _text(link.group("name"))
        for link in _AUTHOR_LINK_RE.finditer(match.group("body"))
    ]
    names = [name for name in names if name]
    return ", ".join(names) if names else _text(match.group("body"))


def _parse_entry(
    dt_html: str,
    dd_html: str,
    *,
    category: str,
    section: str,
    announced: str | None,
    url: str,
) -> dict[str, Any]:
    paper_id = ""
    version = None
    id_match = _ID_RE.search(dt_html) or _ID_RE.search(dd_html)
    if id_match:
        bare = _BARE_ID_RE.match(id_match.group("href").strip())
        if bare:
            paper_id = bare.group("id")
            version = int(bare.group("version")) if bare.group("version") else None
        else:
            paper_id = id_match.group("href").strip()

    title = _first(_TITLE_RE, dd_html)
    abstract = _first(_ABSTRACT_RE, dd_html)
    authors = _authors(dd_html)
    comments = _first(_COMMENTS_RE, dd_html)
    subjects_match = _SUBJECTS_RE.search(dd_html)
    subjects_html = subjects_match.group("body") if subjects_match else ""

    categories: list[str] = []
    for code in _CATEGORY_CODE_RE.findall(subjects_html):
        if code not in categories:
            categories.append(code)
    primary_match = _PRIMARY_RE.search(subjects_html)
    primary = None
    if primary_match:
        primary_codes = _CATEGORY_CODE_RE.findall(primary_match.group("body"))
        primary = primary_codes[0] if primary_codes else None

    return {
        "id": paper_id,
        "title": title,
        "submitted": None,
        "announced": announced,
        # The category whose /new listing we read (the paper may be a cross-list
        # or a replacement announced there), plus every subject code on the page.
        "listing_category": category,
        "categories": categories or ([category] if category else []),
        "primary_category": primary,
        "abstract": abstract,
        "comments": comments,
        "authors": authors,
        "section": section,
        "url": f"https://arxiv.org/abs/{paper_id}" if paper_id else "",
        "source": "arxiv-listing",
    }


def fetch_listing(
    category: str, *, timeout: float = FETCH_TIMEOUT, attempts: int = 2
) -> str:
    """Fetch one category's ``/new`` listing HTML with a browser UA and timeout."""
    url = LISTING_URL.format(category=category)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
    )
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:  # pragma: no cover
            last_error = exc
    raise RuntimeError(f"fetch failed for {url}: {last_error}")


def probe(
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    *,
    out_path: Path | str | None = None,
    fetch=fetch_listing,
    sources: dict[str, str] | None = None,
    assert_reconcile: bool = True,
) -> dict[str, Any]:
    """Fetch+parse every category, write JSONL, and report reconciliation.

    ``sources`` maps a category to already-fetched HTML (used by ``--from-html``
    and by the tests, so no network call is ever required to exercise the parse
    path). Returns a summary dict; raises :class:`ReconciliationError` when
    ``assert_reconcile`` is set and any page fails its own arithmetic.
    """
    pages: list[ListingPage] = []
    problems: list[str] = []
    for category in categories:
        html = sources[category] if sources and category in sources else fetch(category)
        page = parse_listing(html, category)
        pages.append(page)
        problems.extend(page.problems)

    records = [record for page in pages for record in page.records]
    if assert_reconcile and problems:
        raise ReconciliationError(problems)

    if out_path is not None:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    return {
        "categories": list(categories),
        "records": len(records),
        "sections": [
            {"category": page.category, **section.as_dict()}
            for page in pages
            for section in page.sections
        ],
        "problems": problems,
        "out": str(out_path) if out_path is not None else None,
        "records_with_ids": sum(1 for r in records if r.get("id")),
        "records_with_abstracts": sum(1 for r in records if r.get("abstract")),
    }


class ReconciliationError(RuntimeError):
    """Raised when a parsed page does not reconcile with its own printed counts."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _load_sources(paths: Sequence[str]) -> dict[str, str]:
    """Read ``<category>=<html file>`` pairs for offline parsing."""
    sources: dict[str, str] = {}
    for spec in paths:
        category, _, filename = spec.partition("=")
        if not filename:
            raise SystemExit(f"--from-html expects CATEGORY=FILE, got {spec!r}")
        sources[category.strip()] = Path(filename).read_text(encoding="utf-8")
    return sources


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and source-aware-parse arXiv /new listings (#299).",
    )
    parser.add_argument(
        "--categories",
        default=",".join(DEFAULT_CATEGORIES),
        help="comma-separated arXiv categories (default: cs.AI,cs.LG,cs.CL)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="JSONL output path, e.g. ~/.hermes/evolution/research/raw/<date>.jsonl",
    )
    parser.add_argument(
        "--from-html",
        action="append",
        default=None,
        metavar="CATEGORY=FILE",
        help="parse a saved listing page instead of fetching (offline/tests)",
    )
    parser.add_argument(
        "--timeout", type=float, default=FETCH_TIMEOUT, help="per-fetch timeout (s)"
    )
    parser.add_argument(
        "--assert-reconcile",
        action="store_true",
        help="exit non-zero when parsed counts disagree with the page's own totals",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the summary line"
    )
    args = parser.parse_args(argv)

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    sources = _load_sources(args.from_html) if args.from_html else None

    def fetch(category: str) -> str:
        return fetch_listing(category, timeout=args.timeout)

    try:
        summary = probe(
            categories,
            out_path=Path(args.out).expanduser() if args.out else None,
            fetch=fetch,
            sources=sources,
            assert_reconcile=args.assert_reconcile,
        )
    except ReconciliationError as exc:
        print(
            "arXiv listing reconciliation FAILED — refusing the partial parse:",
            file=sys.stderr,
        )
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"arXiv listing probe failed: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            "[arxiv-listing] "
            + json.dumps(
                {
                    "records": summary["records"],
                    "with_ids": summary["records_with_ids"],
                    "with_abstracts": summary["records_with_abstracts"],
                    "sections": len(summary["sections"]),
                    "out": summary["out"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
