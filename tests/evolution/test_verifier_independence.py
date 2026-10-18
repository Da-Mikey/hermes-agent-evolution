# -*- coding: utf-8 -*-
"""Verifier independence from a candidate's own rationale (#213).

Source: arXiv 2610.12822, *Reward Hacking as a Transmissible Strategy in
Multi-Agent Coding Systems* — a candidate that learns "say what the rubric
likes" carries that strategy in the artifact AND its rationale, survives
aggregation, and biases which candidates advance.

Two properties are pinned here:

1. no proposer-authored justification reaches the verifier that scores it
   (asserted with a sentinel embedded in the rationale), and
2. a promotion whose rationale re-encodes an already-rejected candidate's is
   flagged instead of auto-promoted.
"""

import json

import pytest

from evolution.lib.autoharness import build_harness_synthesis_prompt
from evolution.lib.skill_reuse_gate import SkillReuseGate, scan_skill_for_misevolution
from evolution.lib.sub_skill_transfer import SubSkill, SubSkillProvenance, SubSkillStore
from evolution.lib.verifier_independence import (
    RejectedRationaleIndex,
    build_verifier_bundle,
    contains_rationale_key,
    is_rationale_heading,
    rationale_fingerprint,
    rationale_similarity,
    strip_rationale_fields,
    strip_rationale_sections,
)

SENTINEL = "ZQX-SENTINEL-PITCH-4417"

SKILL_MARKDOWN = f"""# PDF form filler

Fills AcroForm PDF fields from a JSON manifest.

## Capabilities
- Read field names from an AcroForm PDF
- Fill fields from a JSON manifest
- Save the filled document

## Rationale
This skill is strictly better than every alternative and any reviewer who
disagrees is missing the point. {SENTINEL}

### Why now
Because the quarter ends soon. {SENTINEL}

## Usage
Run it with a manifest path.
"""


# ── stripping ─────────────────────────────────────────────────────────────


def test_rationale_headings_are_recognised():
    assert is_rationale_heading("Rationale")
    assert is_rationale_heading("Why this matters to our stack")
    assert is_rationale_heading("Value Proposition")
    assert is_rationale_heading("Impact")
    assert not is_rationale_heading("Capabilities")
    assert not is_rationale_heading("Usage")


def test_strip_removes_the_whole_section_including_nested_headings():
    clean, removed = strip_rationale_sections(SKILL_MARKDOWN)

    assert SENTINEL not in clean
    assert "## Rationale" not in clean
    assert "### Why now" not in clean
    # the specification survives
    assert "- Fill fields from a JSON manifest" in clean
    assert "## Usage" in clean
    # the nested heading is swallowed with its parent, not listed twice
    assert removed == ["Rationale"]


def test_strip_leaves_text_without_rationale_byte_identical():
    text = "# Title\n\n## Capabilities\n- do a thing\n"
    assert strip_rationale_sections(text) == (text, [])


def test_strip_fields_handles_dicts_and_nesting():
    candidate = {
        "name": "pdf-form-filler",
        "rationale": SENTINEL,
        "why_now": SENTINEL,
        "evidence": {"run": "ok", "justification": SENTINEL},
    }
    clean = strip_rationale_fields(candidate)

    assert SENTINEL not in json.dumps(clean)
    assert clean["name"] == "pdf-form-filler"
    assert clean["evidence"] == {"run": "ok"}
    assert not contains_rationale_key(clean)


# ── acceptance 1: the verifier never sees the pitch ───────────────────────


def test_harness_synthesis_prompt_carries_no_rationale():
    prompt = build_harness_synthesis_prompt(SKILL_MARKDOWN)

    assert SENTINEL not in prompt, "the proposer's own pitch reached its scorer"
    assert "strictly better than every alternative" not in prompt
    # the spec it is entitled to read is still there
    assert "Fill fields from a JSON manifest" in prompt


def test_harness_synthesis_prompt_strips_dict_rationale_keys():
    prompt = build_harness_synthesis_prompt({
        "name": "pdf-form-filler",
        "description": "Fills AcroForm PDF fields from a JSON manifest.",
        "rationale": SENTINEL,
        "why_it_matters": SENTINEL,
        "capabilities": ["Read field names from an AcroForm PDF"],
    })
    assert SENTINEL not in prompt
    assert "Read field names from an AcroForm PDF" in prompt


def test_verifier_bundle_is_task_edit_evidence_only():
    candidate = {
        "task": "make the filler handle checkboxes",
        "edit": "if field.type == 'checkbox': tick()",
        "evidence": {"tests": "green", "rationale": SENTINEL},
        "rationale": SENTINEL,
        "why_now": SENTINEL,
    }
    bundle = build_verifier_bundle(candidate)

    assert set(bundle) == {"task", "edit", "evidence"}
    assert SENTINEL not in json.dumps(bundle)
    assert bundle["edit"] == "if field.type == 'checkbox': tick()"
    assert bundle["evidence"] == {"tests": "green"}


def test_verifier_bundle_refuses_an_empty_candidate():
    with pytest.raises(ValueError):
        build_verifier_bundle({})


# ── fingerprinting ────────────────────────────────────────────────────────


def test_fingerprint_is_stable_and_similarity_bounds():
    a = "the change removes the retry loop so timeouts stop doubling the work"
    b = "the change removes the retry loop so timeouts stop doubling the workload"

    assert rationale_fingerprint(a) == rationale_fingerprint(a)
    assert rationale_fingerprint(a) != rationale_fingerprint(b)
    assert rationale_similarity(a, a) == 1.0
    assert rationale_similarity(a, b) > 0.6, "light word swap must still match"
    assert rationale_similarity(a, "unrelated tick tock around the garden") < 0.2
    assert rationale_fingerprint("") == ""
    assert rationale_similarity("", a) == 0.0


def test_rejected_index_flags_a_re_encoding():
    index = RejectedRationaleIndex()
    index.add("cand-1", "adds a cache layer because reviewers like caches")

    match = index.best_match("adds a cache layer because the reviewers like a cache")
    assert match is not None
    assert match.candidate_id == "cand-1"
    assert match.similarity >= index.threshold
    assert index.check("adds a cache layer because reviewers like caches")
    assert index.best_match("converts the manifest parser to streaming") is None


def test_rejected_index_round_trips_through_json():
    index = RejectedRationaleIndex(threshold=0.7)
    index.add("cand-1", "a rejected rationale about retries and backoff windows")
    restored = RejectedRationaleIndex.from_dict(json.loads(json.dumps(index.to_dict())))

    assert restored.threshold == 0.7
    assert len(restored) == 1
    assert restored.best_match("a rejected rationale about retries and backoff windows")


def test_rejected_index_rejects_a_nonsense_threshold():
    with pytest.raises(ValueError):
        RejectedRationaleIndex(threshold=0)


# ── the gate: re-encoding is flagged, not promoted ────────────────────────


def test_misevolution_scan_flags_re_encoding_only_when_an_index_is_supplied():
    index = RejectedRationaleIndex()
    index.add("cand-1", "ship it because the benchmark numbers look good to us")

    text = "ship it because the benchmark numbers look good to us"
    assert scan_skill_for_misevolution(text) == (True, [])

    safe, reasons = scan_skill_for_misevolution(text, rationale_index=index)
    assert safe is False
    assert any("re-encodes rejected candidate" in r for r in reasons)


def test_reuse_gate_quarantines_a_re_encoded_rationale(tmp_path):
    index = RejectedRationaleIndex()
    index.add("cand-1", "ship it because the benchmark numbers look good to us")
    gate = SkillReuseGate(tmp_path / "q", tmp_path / "v", rationale_index=index)

    verdict = gate.evaluate(
        "malicious-cache", "ship it because the benchmark numbers look good to us"
    )
    assert verdict.safe is False
    assert verdict.quarantined is True


# ── acceptance 2: promotion is withheld ───────────────────────────────────


def _sub_skill(skill_id, description, *, status="candidate"):
    return SubSkill(
        sub_skill_id=skill_id,
        name=skill_id,
        description=description,
        preconditions=[],
        action_template="run the thing",
        provenance=SubSkillProvenance(session_id="s1", task_id="t1", extracted_at=0),
        status=status,
    )


REJECTED_RATIONALE = "this works because the benchmark numbers look good to us"


def test_promotion_is_withheld_when_the_rationale_re_encodes_a_rejected_one(tmp_path):
    store = SubSkillStore(tmp_path / "sub_skills.jsonl")
    store.add(_sub_skill("rejected-one", REJECTED_RATIONALE))
    store.quarantine("rejected-one", "unsafe")
    store.add(_sub_skill("retry-of-it", REJECTED_RATIONALE))

    promoted = store.promote("retry-of-it")

    assert promoted is False
    assert store.get("retry-of-it").status == "candidate"
    flags = store.promotion_flags()
    assert "retry-of-it" in flags
    assert flags["retry-of-it"]["candidate_id"] == "rejected-one"
    assert flags["retry-of-it"]["similarity"] >= 0.6


def test_promotion_proceeds_for_an_independent_rationale(tmp_path):
    store = SubSkillStore(tmp_path / "sub_skills.jsonl")
    store.add(_sub_skill("rejected-one", REJECTED_RATIONALE))
    store.quarantine("rejected-one", "unsafe")
    store.add(_sub_skill("different-idea", "parses the manifest into typed records"))

    assert store.promote("different-idea") is True
    assert store.get("different-idea").status == "approved"
    assert store.promotion_flags() == {}
