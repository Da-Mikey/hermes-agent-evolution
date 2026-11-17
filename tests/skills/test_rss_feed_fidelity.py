"""Feed fidelity: a full window vs a quiet one, and an edit time vs a publish time.

Two faults pinned here, both measured in the wild on GitHub atom feeds:

* #404 — ``releases.atom`` returns exactly ten entries, HTTP 200, no pagination
  link. A full feed therefore printed exactly like a quiet one, and a released
  version was reported one day and invisible the next.
* #410 — GitHub emits no ``<published>`` at all, only ``<updated>``, which moves
  whenever an entry body is edited. The reader reported that edit time under the
  name ``published``, so an edited old release re-surfaced as news.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "research"
    / "rss-feeds"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import feed  # noqa: E402

# Verbatim shape of https://github.com/vllm-project/vllm/releases.atom: the feed
# header carries a tag time older than the entry's edited body, and there is no
# <published> anywhere in the document.
ATOM_NO_PUBLISHED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from demo</title>
  <updated>2026-11-16T04:27:15Z</updated>
  <entry>
    <id>tag:github.com,2008:Repository/1/v0.34.0</id>
    <updated>2026-11-16T23:50:15Z</updated>
    <title>v0.34.0</title>
    <link rel="alternate" href="https://example.invalid/v0.34.0"/>
    <content type="html">1,061 commits</content>
  </entry>
</feed>"""


def _atom_feed(count: int, *, with_published: bool = True) -> bytes:
    entries = []
    for index in range(count):
        stamp = f"2026-11-{10 + index:02d}T00:00:00Z"
        published = f"<published>{stamp}</published>" if with_published else ""
        entries.append(
            f"<entry><id>tag:example,2008:repo/b{index}</id>"
            f"<title>build {index}</title>"
            f'<link rel="alternate" href="https://example.invalid/{index}"/>'
            f"{published}<updated>{stamp}</updated></entry>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>demo</title>'
        "<updated>2026-11-20T00:00:00Z</updated>" + "".join(entries) + "</feed>"
    ).encode()


# --- #410: an edit time is never a publish time -----------------------------


def test_edit_time_is_not_a_publish_time():
    """Fails before the fix: <updated> used to be reported as `published`."""
    entry = feed.parse_feed(ATOM_NO_PUBLISHED, "application/atom+xml")["entries"][0]
    assert entry["published"] is None, (
        "an <updated> value was reported as a publish time"
    )
    assert entry["updated"] == "2026-11-16T23:50:15+00:00"
    assert entry["id"] == "tag:github.com,2008:Repository/1/v0.34.0"


def test_since_does_not_window_an_undated_entry_as_if_it_were_published():
    """A GitHub release with no publish time is counted, not silently re-dated."""
    entries = feed.parse_feed(ATOM_NO_PUBLISHED, "application/atom+xml")["entries"]
    selected, stats = feed.select_entries(entries, limit=10, since="2026-11-16")
    assert selected == []
    assert stats["undated_excluded"] == 1
    assert stats["entries_seen"] == 1


def test_age_by_updated_is_opt_in_and_reaches_the_record():
    entries = feed.parse_feed(ATOM_NO_PUBLISHED, "application/atom+xml")["entries"]
    selected, stats = feed.select_entries(
        entries, limit=10, since="2026-11-16", age_by="updated"
    )
    assert [e["title"] for e in selected] == ["v0.34.0"]
    assert stats["age_by"] == "updated"


def test_rss_pubdate_still_wins_and_json_feed_splits_its_two_dates():
    """The split must not lose the dates the other two formats genuinely carry."""
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>B</title>'
        b"<item><title>P</title><link>https://ex.invalid/p</link>"
        b"<pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate></item>"
        b"</channel></rss>"
    )
    entry = feed.parse_feed(rss)["entries"][0]
    assert entry["published"] == "2026-11-16T10:00:00+00:00"
    assert entry["updated"] is None

    jsonfeed = (
        b'{"version":"https://jsonfeed.org/version/1.1","title":"J","items":['
        b'{"id":"7","url":"https://ex.invalid/j","title":"J1",'
        b'"date_published":"2026-11-10T00:00:00Z","date_modified":"2026-11-16T00:00:00Z"}]}'
    )
    entry = feed.parse_feed(jsonfeed, "application/feed+json")["entries"][0]
    assert entry["published"] == "2026-11-10T00:00:00+00:00"
    assert entry["updated"] == "2026-11-16T00:00:00+00:00"


# --- #404: a full window is announced, not mistaken for a quiet one ---------


def test_a_full_capped_feed_reports_truncation_and_the_counts():
    entries = feed.parse_feed(_atom_feed(10))["entries"]
    selected, stats = feed.select_entries(entries, limit=3, since=None)
    assert len(selected) == 3
    assert stats == {
        "age_by": "published",
        "entries_seen": 10,
        "entries_shown": 3,
        "dropped_by_limit": 7,
        "dropped_by_since": 0,
        "undated_excluded": 0,
    }
    cap = feed.feed_cap("https://github.com/ggml-org/llama.cpp/releases.atom")
    assert cap == 10
    assert stats["entries_seen"] >= cap


def test_known_cap_table_matches_github_and_is_overridable():
    base = "https://github.com/owner/repo"
    assert feed.feed_cap(f"{base}/releases.atom") == 10
    assert (
        feed.feed_cap(f"{base}/commits/main.atom") == 10
    )  # GitHub /commits/<BRANCH>.atom
    assert feed.feed_cap(f"{base}/tags.atom") == 10
    assert feed.feed_cap("https://hnrss.org/frontpage") is None
    assert feed.feed_cap("https://hnrss.org/frontpage", override=5) == 5


def test_text_output_states_how_many_of_how_many():
    entries = feed.parse_feed(_atom_feed(10))["entries"]
    selected, stats = feed.select_entries(entries, limit=3, since=None)
    record = {
        "title": "demo",
        "format": "atom",
        "url": "https://github.com/owner/repo/releases.atom",
        "entries": selected,
        **stats,
        "cap": 10,
        "truncated": stats["entries_seen"] >= 10,
    }
    text = feed.render_text(record)
    assert "showing 3 of 10 entries" in text
    assert "dropped by --limit" in text
    assert "documented cap" in text
    assert "absence is not evidence of none" in text


def test_an_undated_entry_is_labelled_not_dated_from_its_edit():
    entries = feed.parse_feed(ATOM_NO_PUBLISHED, "application/atom+xml")["entries"]
    record = {
        "title": "demo",
        "format": "atom",
        "url": "https://example.invalid/releases.atom",
        "entries": entries,
        "age_by": "published",
        "entries_seen": 1,
        "entries_shown": 1,
    }
    text = feed.render_text(record)
    assert "undated (edited 2026-11-16)" in text
    assert "- 2026-11-16  v0.34.0" not in text


@pytest.mark.parametrize("count,expected_truncated", [(10, True), (9, False)])
def test_json_record_exposes_seen_shown_and_truncated(count, expected_truncated):
    """The three fields a consumer needs to know whether absence means anything."""
    record = feed.parse_feed(_atom_feed(count))
    selected, stats = feed.select_entries(record["entries"], limit=20, since=None)
    record["entries"] = selected
    record.update(stats)
    cap = feed.feed_cap("https://github.com/owner/repo/releases.atom")
    record["cap"] = cap
    record["truncated"] = cap is not None and stats["entries_seen"] >= cap
    assert record["truncated"] is expected_truncated
    assert record["entries_seen"] == count
    assert record["entries_shown"] == count
