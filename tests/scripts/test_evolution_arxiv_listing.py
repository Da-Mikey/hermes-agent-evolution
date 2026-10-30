"""Tests for the source-aware arXiv listing probe (#299).

The parse path is exercised OFFLINE against a saved listing fixture — no test
in this file touches the network. The point of the issue was that a listing read
through the generic extractor loses ids and abstracts; these tests pin the
replacement: every parsed record carries an ``id`` and an ``abstract``, and the
parse is asserted against the page's own printed counts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_arxiv_listing import (  # noqa: E402
    ReconciliationError,
    main,
    parse_listing,
    probe,
)

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_listing_cs.AI.html"


@pytest.fixture
def listing_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_listing_keeps_ids_and_abstracts(listing_html: str):
    page = parse_listing(listing_html, "cs.AI")

    assert [record["id"] for record in page.records] == [
        "2610.37720",
        "2610.37721",
        "2610.37722",
        "2610.31100",
        "2610.31101",
    ]
    assert all(record["abstract"] for record in page.records)
    assert all(record["title"] for record in page.records)
    # The listing announcement date is carried on every record.
    assert page.announced == "2026-10-30"
    assert {record["announced"] for record in page.records} == {"2026-10-30"}
    # `submitted` is honestly null: a listing page carries no per-paper
    # submission timestamp (use the arXiv API record when the exact time matters).
    assert {record["submitted"] for record in page.records} == {None}
    assert all(record["source"] == "arxiv-listing" for record in page.records)
    assert page.problems == []


def test_parse_listing_sections_reconcile_with_page_totals(listing_html: str):
    page = parse_listing(listing_html, "cs.AI")

    assert [(s.label, s.declared, s.parsed) for s in page.sections] == [
        ("New submissions", 3, 3),
        ("Cross submissions", 2, 2),
    ]
    assert page.declared_total == 5
    assert page.problems == []


def test_parse_listing_recovers_comments_subjects_and_primary(listing_html: str):
    page = parse_listing(listing_html, "cs.AI")
    by_id = {record["id"]: record for record in page.records}

    first = by_id["2610.37720"]
    assert first["comments"] == "31 pages, 7 figures, 10 tables"
    assert first["authors"] == "Jiaming Hu, Zhida Jiang"
    assert first["categories"] == ["cs.AI", "cs.DC", "cs.LG"]
    assert first["primary_category"] == "cs.AI"
    assert first["listing_category"] == "cs.AI"
    assert first["section"] == "New submissions"
    assert first["url"] == "https://arxiv.org/abs/2610.37720"
    assert "Sparse Mixture-of-Experts" in first["abstract"]

    # An entry with no Comments line still parses, with an empty string.
    assert by_id["2610.37721"]["comments"] == ""
    # HTML entities in titles are decoded, not left escaped.
    assert "Rationing < Caching < Planning" in by_id["2610.37721"]["title"]


def test_parse_listing_strips_version_suffix(listing_html: str):
    page = parse_listing(listing_html, "cs.AI")
    ids = {record["id"] for record in page.records}

    assert "2610.37721" in ids  # fixture links /abs/2610.37721v2
    assert not any("v2" in paper_id for paper_id in ids)


def test_parse_listing_flags_count_mismatch(listing_html: str):
    # Claim 4 new submissions while the block still holds 3 entries.
    broken = listing_html.replace(
        "New submissions (showing 3 of 3 entries)",
        "New submissions (showing 4 of 4 entries)",
    ).replace("Total of 5 entries", "Total of 6 entries")

    page = parse_listing(broken, "cs.AI")

    assert any(
        "New submissions: parsed 3 entries, page declares 4" in problem
        for problem in page.problems
    )


def test_parse_listing_flags_missing_abstract(listing_html: str):
    broken = listing_html.replace(
        "We study how an agent should ration tool calls across a long horizon. "
        "A planning-first policy dominates both caching and fixed rationing on "
        "three benchmark suites.",
        "",
    )

    page = parse_listing(broken, "cs.AI")

    assert any("record without an abstract" in problem for problem in page.problems)


def test_probe_writes_one_jsonl_record_per_paper(listing_html: str, tmp_path: Path):
    out = tmp_path / "raw" / "2026-10-30.jsonl"

    summary = probe(
        ["cs.AI"],
        out_path=out,
        sources={"cs.AI": listing_html},
        assert_reconcile=True,
    )

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 5
    assert summary["records"] == 5
    assert summary["records_with_ids"] == 5
    assert summary["records_with_abstracts"] == 5
    assert all(record["id"] for record in records)


def test_probe_refuses_partial_parse_when_reconciling(
    listing_html: str, tmp_path: Path
):
    out = tmp_path / "raw" / "2026-10-30.jsonl"
    broken = listing_html.replace(
        "Cross submissions (showing 2 of 2 entries)",
        "Cross submissions (showing 2 of 9 entries)",
    )

    with pytest.raises(ReconciliationError) as excinfo:
        probe(["cs.AI"], out_path=out, sources={"cs.AI": broken}, assert_reconcile=True)

    assert "Cross submissions: parsed 2 entries, page declares 9" in str(excinfo.value)
    # A failed reconciliation must not leave a half-trusted file behind.
    assert not out.exists()


def test_probe_records_problems_without_asserting(listing_html: str, tmp_path: Path):
    out = tmp_path / "2026-10-30.jsonl"
    broken = listing_html.replace("Total of 5 entries", "Total of 6 entries")

    summary = probe(
        ["cs.AI"], out_path=out, sources={"cs.AI": broken}, assert_reconcile=False
    )

    assert summary["problems"]
    assert out.exists()


def test_probe_honours_category_count_check_across_categories(listing_html: str):
    summary = probe(
        ["cs.AI", "cs.LG"],
        sources={"cs.AI": listing_html, "cs.LG": listing_html},
        assert_reconcile=True,
    )

    assert summary["records"] == 10
    assert {section["category"] for section in summary["sections"]} == {
        "cs.AI",
        "cs.LG",
    }


def test_cli_parses_saved_html_offline(tmp_path: Path):
    out = tmp_path / "2026-10-30.jsonl"

    code = main([
        "--categories",
        "cs.AI",
        "--from-html",
        f"cs.AI={FIXTURE}",
        "--out",
        str(out),
        "--assert-reconcile",
        "--quiet",
    ])

    assert code == 0
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 5


def test_cli_exits_two_on_reconciliation_mismatch(tmp_path: Path):
    broken = tmp_path / "broken.html"
    broken.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(
            "New submissions (showing 3 of 3 entries)",
            "New submissions (showing 3 of 4 entries)",
        ),
        encoding="utf-8",
    )

    code = main([
        "--categories",
        "cs.AI",
        "--from-html",
        f"cs.AI={broken}",
        "--out",
        str(tmp_path / "out.jsonl"),
        "--assert-reconcile",
        "--quiet",
    ])

    assert code == 2


def test_cli_exits_one_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import evolution_arxiv_listing as mod

    def boom(category: str, **kwargs: object) -> str:
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(mod, "fetch_listing", boom)

    code = main(["--categories", "cs.AI", "--out", str(tmp_path / "out.jsonl")])

    assert code == 1
    assert not (tmp_path / "out.jsonl").exists()


def test_research_gate_runs_the_probe_and_keeps_extractor_fallback():
    """The probe must be wired into the real stage path, not sit unused."""
    repo = Path(__file__).resolve().parents[2]
    gate = (repo / "scripts" / "evolution_research_web_gate.sh").read_text(
        encoding="utf-8"
    )

    assert "evolution_arxiv_listing.py" in gate
    assert "--assert-reconcile" in gate
    # The generic extractor stays as the documented fallback path.
    assert "web_extract" in gate

    skill = (
        repo / "skills" / "evolution" / "evolution-research" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "research/raw/" in skill
    assert "web_extract" in skill
