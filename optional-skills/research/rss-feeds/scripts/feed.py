#!/usr/bin/env python3
"""Read RSS / Atom / JSON Feed sources and discover feeds behind a page URL.

Standard library only, so it runs in any Hermes environment without an install
step. Output is JSON (``--json``) or a compact text listing.

    python3 feed.py read https://example.com/feed.xml [--limit N] [--since 2026-09-01]
    python3 feed.py discover https://example.com/
    python3 feed.py read https://example.com/  ->  discovers, then reads the first feed
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "hermes-agent/1.0 (rss-feeds skill; +https://github.com/NousResearch/hermes-agent)"
TIMEOUT = 20
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json")
COMMON_FEED_PATHS = ("/feed", "/feed.xml", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.json", "/blog/feed", "/blog/rss.xml")
# Feeds that hand back a fixed number of entries and no pagination link. When
# one of these is full it reads exactly like a quiet feed, so the caller has to
# be told which it is: absence inside the window is not evidence of absence.
KNOWN_FEED_CAPS = {"releases.atom": 10, "commits.atom": 10, "tags.atom": 10}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def parse_date(value: str | None) -> str | None:
    """Normalise RFC 822 (RSS) and ISO 8601 (Atom/JSON Feed) dates to UTC ISO."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _text(el, *paths) -> str | None:
    for p in paths:
        found = el.find(p, NS)
        if found is not None and (found.text or "").strip():
            return found.text
    return None


def _atom_link(entry) -> str | None:
    alternate = None
    for link in entry.findall("atom:link", NS) + entry.findall("link"):
        href = link.get("href")
        if not href:
            continue
        rel = link.get("rel", "alternate")
        if rel == "alternate":
            return href
        alternate = alternate or href
    return alternate


def parse_xml(data: bytes) -> dict:
    root = ET.fromstring(data)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag == "feed":  # Atom
        title = strip_html(_text(root, "atom:title"))
        entries = []
        for e in root.findall("atom:entry", NS):
            entries.append({
                "id": _text(e, "atom:id"),
                "title": strip_html(_text(e, "atom:title")),
                "link": _atom_link(e),
                # GitHub atom feeds emit no <published> at all: only <updated>,
                # which moves whenever an entry body is edited. Keeping the two
                # apart means an edit time can never be read as a publish time.
                "published": parse_date(_text(e, "atom:published")),
                "updated": parse_date(_text(e, "atom:updated")),
                "author": strip_html(_text(e, "atom:author/atom:name", "dc:creator")),
                "summary": strip_html(_text(e, "atom:summary", "atom:content"))[:2000],
            })
        return {"format": "atom", "title": title, "entries": entries}
    channel = root.find("channel") if tag == "rss" else root  # RSS 2.0 vs RDF/RSS 1.0
    if channel is None:
        raise ValueError(f"unrecognised XML root <{tag}>")
    entries = []
    for item in channel.iter("item") if tag == "rss" else root.iter("{http://purl.org/rss/1.0/}item"):
        entries.append({
            "id": (_text(item, "guid", "{http://purl.org/rss/1.0/}identifier") or "").strip() or None,
            "title": strip_html(_text(item, "title", "{http://purl.org/rss/1.0/}title")),
            "link": (_text(item, "link", "{http://purl.org/rss/1.0/}link") or "").strip() or None,
            "published": parse_date(_text(item, "pubDate", "dc:date")),
            "updated": parse_date(_text(item, "atom:updated")),
            "author": strip_html(_text(item, "dc:creator", "author")),
            "summary": strip_html(_text(item, "content:encoded", "description", "{http://purl.org/rss/1.0/}description"))[:2000],
        })
    return {"format": "rss", "title": strip_html(_text(channel, "title", "{http://purl.org/rss/1.0/}title")), "entries": entries}


def parse_json_feed(data: bytes) -> dict:
    doc = json.loads(data)
    entries = []
    for item in doc.get("items", []):
        authors = item.get("authors") or ([item["author"]] if item.get("author") else [])
        entries.append({
            "id": item.get("id"),
            "title": strip_html(item.get("title")),
            "link": item.get("url") or item.get("external_url"),
            "published": parse_date(item.get("date_published")),
            "updated": parse_date(item.get("date_modified")),
            "author": ", ".join(a.get("name", "") for a in authors if isinstance(a, dict)) or None,
            "summary": strip_html(item.get("summary") or item.get("content_text") or item.get("content_html"))[:2000],
        })
    return {"format": "jsonfeed", "title": strip_html(doc.get("title")), "entries": entries}


def parse_feed(data: bytes, content_type: str = "") -> dict:
    head = data.lstrip()[:1]
    if head == b"{" or "json" in content_type:
        return parse_json_feed(data)
    return parse_xml(data)


def discover(page_url: str, page_html: bytes | None = None) -> list[str]:
    """Return candidate feed URLs for a page: <link rel=alternate> first, then well-known paths."""
    if page_html is None:
        page_html, _ = fetch(page_url)
    text = page_html.decode("utf-8", "replace")
    found: list[str] = []
    for m in re.finditer(r"<link\b[^>]*>", text, re.I):
        tag = m.group(0)
        type_m = re.search(r"""type\s*=\s*["']([^"']+)""", tag, re.I)
        href_m = re.search(r"""href\s*=\s*["']([^"']+)""", tag, re.I)
        rel_m = re.search(r"""rel\s*=\s*["']([^"']+)""", tag, re.I)
        if not href_m or not type_m or type_m.group(1).lower() not in FEED_TYPES:
            continue
        if rel_m and "alternate" not in rel_m.group(1).lower():
            continue
        url = urllib.parse.urljoin(page_url, html.unescape(href_m.group(1)))
        if url not in found:
            found.append(url)
    if found:
        return found
    parsed = urllib.parse.urlsplit(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return [base + p for p in COMMON_FEED_PATHS]


def looks_like_feed(data: bytes, content_type: str) -> bool:
    head = data.lstrip()[:300].lower()
    return head.startswith(b"{") and b"items" in data[:2000] or b"<rss" in head or b"<feed" in head or b"<rdf" in head


def read(url: str) -> dict:
    data, ctype = fetch(url)
    if looks_like_feed(data, ctype):
        feed = parse_feed(data, ctype)
        feed["url"] = url
        return feed
    candidates = discover(url, data)
    errors = []
    for cand in candidates:
        try:
            cdata, cctype = fetch(cand)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"{cand}: {exc}")
            continue
        if looks_like_feed(cdata, cctype):
            feed = parse_feed(cdata, cctype)
            feed["url"] = cand
            feed["discovered_from"] = url
            return feed
    raise SystemExit(f"no feed found at {url}; tried {len(candidates)} candidates\n" + "\n".join(errors))


def feed_cap(url: str, override: int | None = None) -> int | None:
    """Entry cap this URL is known to impose, or None when it is not capped."""
    if override:
        return override
    path = urllib.parse.urlsplit(url).path
    for suffix, cap in KNOWN_FEED_CAPS.items():
        if path.endswith(suffix):
            return cap
    # GitHub's commits feed is /OWNER/REPO/commits/<BRANCH>.atom — no fixed suffix.
    if "/commits/" in path and path.endswith(".atom"):
        return KNOWN_FEED_CAPS["commits.atom"]
    return None


def entry_date(entry: dict, age_by: str = "published") -> str | None:
    """The timestamp an entry is aged by, or None when the feed gave none."""
    if age_by == "updated":
        return entry.get("updated") or entry.get("published")
    return entry.get("published")


def _cutoff(since: str) -> datetime:
    cutoff = (
        datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        if "T" not in since
        else datetime.fromisoformat(since.replace("Z", "+00:00"))
    )
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def select_entries(
    entries: list[dict], limit: int, since: str | None, age_by: str = "published"
) -> tuple[list[dict], dict]:
    """Window, sort and slice, and report what was left out and why.

    A feed that just returned a full ten-entry atom window and one that is
    genuinely quiet both print as ten lines. Only the counts let a reader tell
    them apart, so they travel with the entries. Entries carrying no timestamp
    for ``age_by`` are reported as ``undated_excluded`` rather than dropped
    silently.
    """
    seen = len(entries)
    undated = 0
    if since:
        cutoff = _cutoff(since)
        windowed = []
        for entry in entries:
            stamp = entry_date(entry, age_by)
            if not stamp:
                undated += 1
                continue
            if datetime.fromisoformat(stamp) >= cutoff:
                windowed.append(entry)
    else:
        windowed = list(entries)
    windowed.sort(key=lambda e: entry_date(e, age_by) or "", reverse=True)
    selected = windowed[:limit]
    stats = {
        "age_by": age_by,
        "entries_seen": seen,
        "entries_shown": len(selected),
        "dropped_by_limit": len(windowed) - len(selected),
        "dropped_by_since": seen - len(windowed) - undated,
        "undated_excluded": undated,
    }
    return selected, stats


def filter_entries(entries: list[dict], limit: int, since: str | None) -> list[dict]:
    """The selection alone, without the counts (kept for existing callers)."""
    selected, _ = select_entries(entries, limit, since)
    return selected


def render_text(feed: dict) -> str:
    age_by = feed.get("age_by", "published")
    seen = feed.get("entries_seen")
    shown = feed.get("entries_shown", len(feed["entries"]))
    lines = [f"{feed.get('title') or '(untitled feed)'}  [{feed['format']}]  {feed['url']}"]
    if seen is not None:
        counts = f"showing {shown} of {seen} entries (aged by {age_by})"
        if feed.get("dropped_by_limit"):
            counts += f"; {feed['dropped_by_limit']} dropped by --limit"
        if feed.get("dropped_by_since"):
            counts += f"; {feed['dropped_by_since']} older than --since"
        if feed.get("undated_excluded"):
            counts += f"; {feed['undated_excluded']} undated, excluded by --since"
        lines.append(counts)
    if feed.get("truncated"):
        lines.append(
            f"warning: this feed returned {seen} entries, its documented cap — the window is"
            " full, so anything older is not visible here and absence is not evidence of none"
        )
    for e in feed["entries"]:
        when = (entry_date(e, age_by) or "")[:10]
        if not when:
            when = f"undated (edited {e['updated'][:10]})" if e.get("updated") else "undated"
        by = f"  — {e['author']}" if e.get("author") else ""
        lines.append(f"- {when}  {e['title'] or '(no title)'}{by}\n  {e['link'] or ''}")
        if e.get("summary"):
            lines.append(f"  {e['summary'][:300]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="read a feed (or discover one behind a page URL)")
    r.add_argument("url")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--since", help="ISO date/datetime; drop older entries")
    r.add_argument(
        "--age-by",
        choices=("published", "updated"),
        default="published",
        help="timestamp used for --since and ordering (default: published)",
    )
    r.add_argument("--cap", type=int, help="override the feed's known entry cap")
    r.add_argument("--json", action="store_true")
    d = sub.add_parser("discover", help="list feed URLs advertised by a page")
    d.add_argument("url")
    d.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "discover":
            urls = discover(args.url)
            print(json.dumps(urls, indent=2) if args.json else "\n".join(urls))
            return 0
        feed = read(args.url)
        cap = feed_cap(feed["url"], args.cap)
        feed["entries"], stats = select_entries(
            feed["entries"], args.limit, args.since, args.age_by
        )
        feed.update(stats)
        feed["cap"] = cap
        feed["truncated"] = cap is not None and stats["entries_seen"] >= cap
        print(json.dumps(feed, indent=2, ensure_ascii=False) if args.json else render_text(feed))
        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} for {exc.url}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, ET.ParseError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
