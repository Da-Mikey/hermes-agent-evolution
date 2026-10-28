#!/usr/bin/env python3
"""Deterministic pre-extract for evolution-introspection (#89).

evolution-introspection previously loaded RAW session transcripts (last 7 days)
into the LLM context — unbounded megabytes, the single largest context bomb in
the pipeline, AND it put the user's private text into the model context.

This script (no LLM) scans the session files for PROBLEM SIGNALS only and emits
a compact, ANONYMIZED digest — counts per signal/tool, generic shapes, never
raw content. The skill feeds ONLY this digest to the model. Raw private text
never enters the context (complements the PII redaction gate #82).

Three on-disk session formats are scanned: the upstream ``*.jsonl``
transcripts, ``request_dump_*.json`` snapshots (#238), and the SQLite
SessionDB ``state.db`` messages table (#399). A request dump carries the same
role-tagged messages at ``request.body.messages`` plus a provider ``error``
object; ignoring it left those installs reporting ``sessions_scanned: 0`` and
blinded the whole self-improvement loop. The SessionDB is where >90% of real
sessions live, so the messages table is read, grouped by session_id and ordered
by id, then passed through the same scan_messages path.

Signals extracted:
  * tool_failures  — tool results that look like failures, attributed to the
    tool (via tool_call_id -> name from the preceding assistant turn). Reuses
    agent.loop_guard's failure markers for consistency.
  * timeouts       — results mentioning timeout / timed out.
  * refusals       — assistant text expressing "I can't / no access / denied".
  * refusal_recovery — the subset of those refusals that also proposed an
    alternative path ("...but I can do X instead"), plus the resulting rate.
    Measures whether the recovery-before-refusal guidance (#1356) is working;
    without it a bare refusal and a recovered one are indistinguishable.
  * repeated_tool_runs — same tool called many times consecutively (the spiral
    shape loop_guard guards against), counted per session.
  * provider_errors — from request_dump error objects: ``status_code:type``
    only (never the response body/text, which can echo private content).
  * models_used    — the model id from each request dump (anonymized metadata).

Output: a JSON digest to stdout (and optionally a file), a few KB max.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_constants import get_hermes_home


def _tool_result_failed(content: Any) -> bool:
    """Structural failure classifier for a tool result (issue #347).

    Every Hermes tool serialises its result as a JSON envelope that already
    carries the authoritative status (``exit_code`` for terminal/code-exec,
    ``error`` and/or ``success``/``status`` for the rest). We read that status
    instead of substring-scanning the body.

    The old substring matcher scanned the WHOLE envelope string for marker
    words ("failed", "error:", "404", "timeout") and fired on file *content*
    returned by SUCCESSFUL calls — e.g. ``read_file`` of a page mentioning
    "HTTP 404", or a ``grep`` whose stdout contains the word "error:". That
    massively over-counted failures and misattributed them to tools with zero
    genuine errors (read_file/skill_view), corrupting the introspection signal.

    A result counts as a failure ONLY when its structured status says so:
      * ``exit_code`` present (terminal/code-exec) → failure iff it is not 0,
        UNLESS the envelope carries an ``exit_code_meaning`` note marking the
        non-zero code as informational (e.g. grep=1 "No matches found (not an
        error)", diff=1 "Files differ", test=1 "condition is false").  The
        terminal tool sets ``exit_code_meaning`` via ``_interpret_exit_code``
        precisely so downstream consumers can distinguish an expected
        non-zero exit from a genuine failure — #2873 part 3.  Without this
        honor, every grep/diff/test "no match" exit was counted as a terminal
        failure, corrupting the introspection signal with false positives;
      * a truthy ``error`` field, or ``status == "error"``;
      * an explicit ``success``/``ok`` field that is falsy.
    A result with no recognised status field — including any non-JSON / plain
    string body — is NOT counted: we no longer guess from content. This trades
    a few genuinely-plain-string failures (rare; tools emit JSON envelopes) for
    eliminating the false-positive flood, exactly as the issue prescribes.
    """
    data = content
    if isinstance(data, (str, bytes)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        stripped = text.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False  # not a JSON envelope → no authoritative status → don't guess
        try:
            data = json.loads(stripped)
        except ValueError:
            return False
    if not isinstance(data, dict):
        return False
    if "exit_code" in data:
        try:
            code = int(data["exit_code"])
        except (TypeError, ValueError):
            return False
        if code == 0:
            return False
        # Honor the terminal tool's informational exit-code note (#2873 part
        # 3).  When the envelope carries ``exit_code_meaning`` the terminal
        # tool has already interpreted the code: grep/diff/test/git return 1
        # for "no matches"/"files differ"/"false" — expected, not a failure.
        meaning = data.get("exit_code_meaning")
        if isinstance(meaning, str) and meaning:
            # The note text from _interpret_exit_code is phrased as "not an
            # error" / "expected" for the informational cases.  Only the
            # informational notes suppress failure counting; genuine errors
            # (permission denied, command not found, timeouts) either have no
            # exit_code_meaning at all or carry a note that does NOT contain
            # the "not an error"/"expected" markers.
            low = meaning.lower()
            if "not an error" in low or "expected" in low:
                return False
        return True
    if data.get("error") or str(data.get("status", "")).lower() == "error":
        return True
    for ok_key in ("success", "ok"):
        if ok_key in data:
            return not bool(data[ok_key])
    return False


_TIMEOUT_RE = re.compile(r"\b(timed out|timeout)\b", re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"\b(i can('|no)?t|cannot|no access|access denied|not permitted|don'?t have (access|permission))\b",
    re.IGNORECASE,
)

# --- refusal-recovery detection (issue #1366, Child B of #1327) --------------
# #1356 told the agent to propose an alternative path before refusing for a
# missing capability. _REFUSAL_RE alone cannot tell whether it did: "I can't do
# that" and "I can't do that directly, but I can via `terminal`" both match it,
# so the 51/week signal could not move even if the guidance worked perfectly.
#
# A turn counts as RECOVERED when it refuses AND offers a concrete alternative.
# Deliberately conservative — these are pivot phrases that introduce a different
# path, not mere hedging. Over-matching would inflate the recovery rate and make
# #1356 look effective when it is not, which is worse than under-counting.
_RECOVERY_RE = re.compile(
    r"\b("
    r"instead[,:]? (i|we|let)|alternatively[,:]? (i|we|let)|"
    r"workaround|another (way|approach|option)|different (way|approach|path)|"
    r"(but|however|though)[^.!?]{0,40}\b(i|we) (can|could|'ll|will|am able to)"
    r"(?!['’]?t\b)|"
    r"what i can do|here'?s what i can|i can (still|however)|"
    r"let me (try|use|write|search|run)|i'?ll (try|use|write|search|run)"
    r")\b",
    re.IGNORECASE,
)

#: How far after the refusal a pivot may sit and still be about it.
#
# Without a bound, the two patterns only had to appear SOMEWHERE in the same
# message. On the real corpus that matched pairs tens of thousands of characters
# apart — long audit and bug-report documents where "cannot" describes some
# third-party system and an unrelated "instead"/"workaround" appears pages later.
# A genuine pivot follows its refusal within a sentence or two.
_RECOVERY_PROXIMITY_CHARS = 240


# Two shapes match the pivot above but are NOT the agent taking another path,
# and counting them would make #1356 look effective precisely when it failed:
#
#   * DEFLECTION — the work is handed back to the human ("however you can try
#     it yourself", "instead, please contact support"). The pivot clause is
#     therefore first-person only above; this vetoes the rest.
#   * EXPLANATION — the agent offers to describe the problem rather than solve
#     it ("but I can tell you why it failed"). An explanation is not an
#     alternative path to the goal.
#
# Vetoing beats narrowing the recovery pattern: the shapes are open-ended, and
# under-counting a real recovery is far cheaper than reporting a refusal rate
# that improves whenever the agent gets more polite about giving up.
_NON_RECOVERY_RE = re.compile(
    r"\b("
    r"contact (support|your|the)|ask (your|someone|a )|"
    r"you (can|could|should|will|may|might|'ll) |your (own|side|end)|"
    r"(do|try|run|check) (it|this|that) yourself|manually|"
    r"(tell|explain to|show) you (why|what|how) (it|this|that)? ?(failed|went|is|does)|"
    r"i can (only )?(tell|explain|describe|suggest that you)"
    r")\b",
    re.IGNORECASE,
)


def _is_recovered_refusal(text: str) -> bool:
    """True when a refusing turn also proposes a path the AGENT will take.

    Requires the pivot to sit within ``_RECOVERY_PROXIMITY_CHARS`` after a
    refusal. Matching anywhere in the message was the dominant false-positive
    source on the real corpus: long audit and bug-report documents contain a
    "cannot" about some third-party system and an unrelated "instead" pages
    later, and were counted as recoveries.

    A pivot BEFORE any refusal does not count either — "Instead, let me check
    the cache. I can't reach the API." is a plan followed by a refusal, not a
    refusal followed by a plan.
    """
    for refusal in _REFUSAL_RE.finditer(text):
        window = text[refusal.end() : refusal.end() + _RECOVERY_PROXIMITY_CHARS]
        if _RECOVERY_RE.search(window) and not _NON_RECOVERY_RE.search(window):
            return True
    return False
    return bool(_RECOVERY_RE.search(text)) and not _NON_RECOVERY_RE.search(text)

_REPEAT_THRESHOLD = 5  # same tool >=N consecutive in a session is a "repeated run"

# --- failure-reason taxonomy (issue #1325) ----------------------------------
# Sub-classifies a *failed* tool result so the introspection loop can attribute
# regressions to the actual failure mode, not just the tool name — fixing the
# fix→regress→refile treadmill where a fix for ``read_file:file-not-found`` is
# credited/blamed for ``read_file:timeout``. No LLM: keyword/field rules over the
# structured ``error``/``exit_code``/status fields. Order matters — first hit
# wins, so the more specific patterns precede the generic ones.
_FILE_NOT_FOUND_RE = re.compile(
    r"\b(no such file|not found|does not exist|cannot find|file missing|not a directory)\b",
    re.IGNORECASE,
)
_PERMISSION_RE = re.compile(
    r"\b(permission denied|access denied|not permitted|operation not permitted|forbidden|403|EACCES)\b",
    re.IGNORECASE,
)
_TIMEOUT_DETAIL_RE = re.compile(
    r"\b(timed out|timeout|deadline exceeded|ETIMEDOUT)\b", re.IGNORECASE
)
_QUOTA_RE = re.compile(
    r"\b(quota|rate limit|too many requests|429|billing|insufficient|exhausted)\b",
    re.IGNORECASE,
)
_PARSE_RE = re.compile(
    r"\b(json|jsondecode|parse error|syntaxerror|valueerror|invalid json|unexpected token|unparsable)\b",
    re.IGNORECASE,
)
_NO_MATCH_RE = re.compile(
    r"\b(no match|no results?|nothing found|empty result|0 matches)\b", re.IGNORECASE
)
# Patch-specific sub-categories (#1501). These are checked BEFORE the
# generic _NO_MATCH_RE so specific patch failures aren't swallowed by
# the broad "no match" bucket. They match the structured diagnostic
# text emitted by tools.fuzzy_match.format_structured_error and the
# raw error strings from fuzzy_find_and_replace.
_PATCH_OLD_STRING_EMPTY_RE = re.compile(
    r"\bold_string (cannot be empty|is empty)\b", re.IGNORECASE
)
_PATCH_INDENTATION_MISMATCH_RE = re.compile(
    r"\bindentation[ _-]mismatch\b|indentation differs\b", re.IGNORECASE
)
_PATCH_AMBIGUOUS_RE = re.compile(
    r"\b(found \d+ matches|multiple matches found)\b", re.IGNORECASE
)
# Patch no-match: the dominant remaining 'other' cause (#1537). The fuzzy
# matcher's failure message is "Could not find a match for old_string in the
# file" (tools/fuzzy_match.py), which does NOT match the generic _NO_MATCH_RE
# (it needs literal "no match"). Without this sub-category, 81% of patch
# failures (70/86 per the 7d introspection window) fell into the opaque
# 'other' bucket, hiding the dominant failure mode from the digest and
# blocking targeted fixes. Placed BEFORE _NO_MATCH_RE so the specific tag
# wins; the message never contained "no match" anyway, but the ordering keeps
# the precedent set by the other patch-* sub-categories.
_PATCH_NO_MATCH_RE = re.compile(
    r"\b(could not find a match|could not find|cannot find a match)\b",
    re.IGNORECASE,
)
# Skill-management sub-categories (#253). The skill tool is the agent's own
# self-improvement path, so an opaque failure there costs more than its count:
# a *skill lookup* miss rendered as ``file-not-found`` (that rule matches a bare
# "not found"), the skill's file miss as the same class, and every write-guard
# refusal as the catch-all ``other``. Measured before this slice: 9 of 10 skill
# failures were indistinguishable. These sit BEFORE the generic rules, so they
# deliberately RE-TAG what fell there — the same re-tag discipline used for
# write_file (#3468) and patch (#1501). Every pattern is skill-scoped on
# purpose: a plain "File not found: x" must stay ``file-not-found``.
_SKILL_NOT_FOUND_RE = re.compile(
    r"\bskill\s+['\"`][^'\"`]+['\"`]\s+not\s+found\b", re.IGNORECASE
)
_SKILL_FILE_NOT_FOUND_RE = re.compile(
    r"\bnot\s+found\s+in\s+skill\s+['\"`]", re.IGNORECASE
)
_SKILL_GATE_REJECTED_RE = re.compile(
    r"\b(validation|provenance)\s+gate\s+rejected\b", re.IGNORECASE
)
_SKILL_CURATOR_REFUSED_RE = re.compile(r"\brefusing\s+background\s+curator\b", re.IGNORECASE)
# write_file-specific sub-categories (#3468). The write refusal guards emit
# canonical phrases that match NO generic regex, so 32/34 write_file failures
# (94% per the 7d window) landed in the opaque 'other' bucket. These mirror the
# reason tags the tool envelope itself now emits (tools/file_tools_write_guards.
# WRITE_REASON_HINTS), so digest reasons and envelope reasons stay the same
# vocabulary. Placed BEFORE _NO_MATCH_RE with the other tool-specific rules.
_WRITE_SENSITIVE_PATH_RE = re.compile(
    r"\brefusing to write to (?:sensitive system path|hermes config file)\b",
    re.IGNORECASE,
)
_WRITE_PROTECTED_INSTRUCTION_RE = re.compile(
    r"\bsteers? future agent behavior\b|\bprotected (?:instruction|agent-instruction) file",
    re.IGNORECASE,
)
_WRITE_BINARY_CONTENT_RE = re.compile(
    r"\brefusing to write plain text to\b|\brefusing to overwrite existing (?:binary|pdf)\b",
    re.IGNORECASE,
)
_WRITE_STALE_OVERWRITE_RE = re.compile(
    r"\bhas not seen its full current content\b|\bwas modified since you last read it\b",
    re.IGNORECASE,
)
_WRITE_INTERNAL_CONTENT_RE = re.compile(
    r"\brefusing to write internal read_file display text\b",
    re.IGNORECASE,
)
_WRITE_CROSS_PROFILE_RE = re.compile(r"\bcross[- ]profile\b", re.IGNORECASE)
_WRITE_PATH_IS_DIRECTORY_RE = re.compile(r"\bis a directory\b", re.IGNORECASE)
_WRITE_CONTENT_TOO_LARGE_RE = re.compile(
    r"\btoo large\b|\bexceeds (?:the )?(?:size|limit)\b|\bsize limit\b",
    re.IGNORECASE,
)

# --- #184: the two paths the digest could not diagnose ----------------------
# 32/46 ``execute_code`` failures and 27/30 deferred-``tool_call`` failures were
# landing in the opaque 'other' bucket with no cause recorded, so the agent
# repeated an identical call instead of correcting it (up to 16 consecutive
# identical calls in one session). Both shapes DO carry their cause; nothing
# was reading it. These rules read it at capture time.
#
# Exception CLASS only, matched case-sensitively: Python exception names are
# CamelCase (``KeyError``, ``ModuleNotFoundError``), so a case-sensitive match
# cannot fire on ALL-CAPS prose ("NO ERROR") the way a folded one would. The
# traceback BODY is deliberately not emitted — see the no-raw-text contract in
# ``_tool_result_failed`` and the test that pins it.
_EXCEPTION_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z_]*(?:Error|Exception|Interrupt))\b")

# The bridge refuses in prose the digest never classified. The reason tag names
# the FAILURE SHAPE (unknown sub-tool / bad arguments / missing field) rather
# than quoting the offending field value: a schema field name is exactly the
# kind of raw text the no-raw-text contract exists to keep out of the digest,
# and the shape is what tells the agent which correction to make.
_BRIDGE_UNKNOWN_TOOL_RE = re.compile(
    r"\b(unknown tool|no such tool|tool (?:not found|is not (?:available|registered))|"
    r"unrecognized (?:tool|tool name))\b",
    re.IGNORECASE,
)
_BRIDGE_MISSING_FIELD_RE = re.compile(
    r"\b(missing required (?:argument|parameter|field)|"
    r"required (?:argument|parameter|field)s? (?:is|are)? ?missing|field required)\b",
    re.IGNORECASE,
)
_BRIDGE_INVALID_ARGS_RE = re.compile(
    r"\b(unexpected keyword argument|invalid (?:arguments|parameters|argument|parameter|schema)|"
    r"arguments? (?:did not|does not) (?:validate|match)|validation (?:error|failed)|"
    r"schema (?:violation|mismatch)|failed to validate)\b",
    re.IGNORECASE,
)


def _classify_failure_reason(content: Any) -> Optional[str]:
    """Sub-classify a *failed* tool result into a coarse reason tag (issue #1325).

    Returns ``None`` when ``content`` is NOT a failure (so callers gate on this
    instead of separately calling ``_tool_result_failed``), otherwise one of the
    taxonomy tags below. Classification is best-effort keyword/field matching
    over the structured envelope only — never over raw user content — so it
    inherits the no-raw-text contract from ``_tool_result_failed``.
    """
    data = content
    if isinstance(data, (str, bytes)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        stripped = text.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return None  # not a JSON envelope → not a structural failure
        try:
            data = json.loads(stripped)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None

    # First — is it a failure at all? Mirror _tool_result_failed exactly.
    is_failed = False
    if "exit_code" in data:
        try:
            is_failed = int(data["exit_code"]) != 0
        except (TypeError, ValueError):
            is_failed = False
    elif data.get("error") or str(data.get("status", "")).lower() == "error":
        is_failed = True
    else:
        for ok_key in ("success", "ok"):
            if ok_key in data:
                is_failed = not bool(data[ok_key])
                break
    if not is_failed:
        return None

    # Build the searchable text from STRUCTURED fields only — error, status,
    # output (terminal stdout/stderr), and the file/path hints tools emit. We
    # deliberately do NOT scan arbitrary content values; this keeps the
    # no-raw-text contract intact while still catching the canonical failure
    # messages tools put in error/output.
    hints: List[str] = []
    for key in ("error", "status", "output", "stderr", "reason"):
        v = data.get(key)
        if isinstance(v, str) and v:
            hints.append(v)
    haystack = " ".join(hints)

    if data.get("reason") == "safety-gate-blocked" or re.search(
        r"tool execution blocked by safety gate:", haystack, re.IGNORECASE
    ):
        return "safety-gate-blocked"
    # #253 — skill management: the self-improvement path. Ordered ahead of the
    # generic file/not-found rules, which match a bare "not found" and would
    # otherwise render a skill *lookup* miss as a filesystem miss.
    if _SKILL_NOT_FOUND_RE.search(haystack):
        return "skill-not-found"
    if _SKILL_FILE_NOT_FOUND_RE.search(haystack):
        return "skill-file-not-found"
    if _SKILL_GATE_REJECTED_RE.search(haystack):
        return "skill-gate-rejected"
    if _SKILL_CURATOR_REFUSED_RE.search(haystack):
        return "skill-curator-refused"
    if _FILE_NOT_FOUND_RE.search(haystack):
        return "file-not-found"
    if _PERMISSION_RE.search(haystack):
        return "permission-denied"
    if _TIMEOUT_DETAIL_RE.search(haystack):
        return "timeout"
    if _QUOTA_RE.search(haystack):
        return "quota/billing"
    if _PARSE_RE.search(haystack):
        return "parse-error"
    if _PATCH_OLD_STRING_EMPTY_RE.search(haystack):
        return "patch-old-string-empty"
    if _PATCH_INDENTATION_MISMATCH_RE.search(haystack):
        return "patch-indentation-mismatch"
    if _PATCH_AMBIGUOUS_RE.search(haystack):
        return "patch-ambiguous"
    if _PATCH_NO_MATCH_RE.search(haystack):
        return "patch-no-match"
    if _WRITE_SENSITIVE_PATH_RE.search(haystack):
        return "write-sensitive-path"
    if _WRITE_PROTECTED_INSTRUCTION_RE.search(haystack):
        return "write-protected-instruction"
    if _WRITE_BINARY_CONTENT_RE.search(haystack):
        return "write-binary-content"
    if _WRITE_STALE_OVERWRITE_RE.search(haystack):
        return "write-stale-overwrite"
    if _WRITE_INTERNAL_CONTENT_RE.search(haystack):
        return "write-internal-content"
    if _WRITE_CROSS_PROFILE_RE.search(haystack):
        return "write-cross-profile"
    if _WRITE_PATH_IS_DIRECTORY_RE.search(haystack):
        return "write-path-is-directory"
    if _WRITE_CONTENT_TOO_LARGE_RE.search(haystack):
        return "write-content-too-large"
    if _NO_MATCH_RE.search(haystack):
        return "no-match"
    # #184 — deferred ``tool_call`` bridge: name the refusal shape. Ordered most
    # specific first. CORRECTED (#184 slice 2, 2026-10-13): the claim that these
    # rules were "entirely additive … no failure that already had a tag is
    # re-tagged" is FALSE, and was measured so before merge. They sit BEFORE the
    # generic ``exit_code`` fallback at the end of this function, so a non-zero
    # exit whose structured text names a bridge shape is re-tagged away from
    # ``non-zero-exit``. What is true, and is pinned by
    # ``test_pre_existing_specific_tag_beats_exception_rule``: every
    # pre-existing *specific* tag matched EARLIER in this function still wins.
    if _BRIDGE_UNKNOWN_TOOL_RE.search(haystack):
        return "bridge-unknown-tool"
    if _BRIDGE_MISSING_FIELD_RE.search(haystack):
        return "bridge-missing-field"
    if _BRIDGE_INVALID_ARGS_RE.search(haystack):
        return "bridge-invalid-args"
    # #184 — ``execute_code`` (and any code-exec path) reports its cause as a
    # Python exception class inside the traceback. Without this the class was
    # invisible: KeyError, ModuleNotFoundError and a plain non-zero exit all
    # rendered as the same tag, so the agent retried rather than corrected.
    # This rule sits BEFORE the ``exit_code`` fallback too, so an exception class
    # deliberately re-tags what would otherwise have rendered as the opaque
    # ``non-zero-exit`` (pinned by
    # ``test_exception_class_precedes_non_zero_exit_fallback``). Every
    # pre-existing *specific* tag still wins, being matched earlier in this
    # function — that is the additive part, and only that part.
    exc = _EXCEPTION_CLASS_RE.search(haystack)
    if exc:
        return f"exception:{exc.group(1)}"
    if "exit_code" in data:
        return "non-zero-exit"
    return "other"


def _iter_lines(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


# Keep the id on DB-derived message dicts so _state_db_session_signals can re-sort.
_MESSAGE_ROW_ID_KEY = "_db_id"


def _message_row_to_dict(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """Convert a SessionDB messages row into the role-tagged dict scan_messages
    consumes (#399). Drops DB-only columns (session_id) but keeps
    the original id for ordering and timestamp for cutoff filtering (#3145)."""
    obj: Dict[str, Any] = {_MESSAGE_ROW_ID_KEY: row["id"]}
    if "role" in row.keys():
        obj["role"] = row["role"]
    if "content" in row.keys():
        obj["content"] = row["content"]
    if "tool_call_id" in row.keys():
        obj["tool_call_id"] = row["tool_call_id"]
    if "tool_calls" in row.keys() and row["tool_calls"] is not None:
        try:
            parsed = json.loads(row["tool_calls"])
            if isinstance(parsed, list):
                obj["tool_calls"] = parsed
        except ValueError:
            pass
    if "tool_name" in row.keys():
        obj["tool_name"] = row["tool_name"]
    if "timestamp" in row.keys() and row["timestamp"] is not None:
        try:
            obj["timestamp"] = float(row["timestamp"])
        except (ValueError, TypeError):
            pass
    return obj if obj.get("role") else None


def _iter_state_db(db_path: Path) -> Iterable[tuple[str, List[Dict[str, Any]]]]:
    """Yield (session_id, messages) from a SQLite state.db messages table.

    Messages are grouped by session_id and ordered by id (insertion order) so
    tool_call_id -> tool name resolution works exactly as it does for JSONL.
    Malformed rows / missing columns are skipped without crashing the scan."""
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Probe schema; the expected columns are id, session_id, role, content,
        # tool_call_id, tool_calls, tool_name, timestamp. Any subset is fine.
        try:
            cur.execute(
                "SELECT session_id, id, role, content, tool_call_id, tool_calls, "
                "tool_name, timestamp FROM messages ORDER BY session_id, id"
            )
        except sqlite3.OperationalError:
            try:
                cur.execute(
                    "SELECT session_id, id, role, content, tool_call_id, tool_calls, "
                    "tool_name FROM messages ORDER BY session_id, id"
                )
            except sqlite3.Error:
                return
        except sqlite3.Error:
            return
        current_session: Optional[str] = None
        current_messages: List[Dict[str, Any]] = []
        for row in cur:
            sid = row["session_id"]
            msg = _message_row_to_dict(row)
            if msg is None:
                continue
            if sid != current_session:
                if current_session is not None:
                    yield current_session, current_messages
                current_session = sid
                current_messages = []
            current_messages.append(msg)
        if current_session is not None:
            yield current_session, current_messages
    finally:
        conn.close()


def _state_db_session_signals(msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return signals from one SessionDB session, ordered by the original id.

    The caller gives us messages already grouped by session_id and ordered by
    id, but we also carry the original id on each dict so we can re-sort here
    as a defense-in-depth step. The id and timestamp keys are stripped before
    scanning so they never leak into the digest."""
    ordered = sorted(msgs, key=lambda m: m.get(_MESSAGE_ROW_ID_KEY, 0))
    for m in ordered:
        m.pop(_MESSAGE_ROW_ID_KEY, None)
        m.pop("timestamp", None)
    return scan_messages(ordered)


def scan_messages(messages) -> Dict[str, Any]:
    """Return per-session signal counts (no raw text) from an iterable of
    role-tagged message dicts. Shared by the JSONL transcript path, the
    request_dump_*.json path (#238), and the SessionDB state.db path (#399) so
    all formats yield the identical digest.
    """
    tool_failures: Counter = Counter()
    tool_failures_by_reason: Dict[
        str, Dict[str, int]
    ] = {}  # tool -> reason -> count (#1325)
    # Per-tool CALL counts (#317) — the denominator a failure *rate* needs.
    # Two independent observations are kept because neither alone is complete:
    #   * declared — tool calls the assistant asked for (a call that crashed
    #     before producing a result still counts as an attempt);
    #   * results  — tool-result rows actually observed (a result whose
    #     originating assistant turn fell outside the scanned slice still
    #     proves a call happened).
    # ``tool_calls`` is the per-tool MAX of the two, which also guarantees
    # failures[tool] <= calls[tool] (every failure is one result row) so a
    # published rate can never exceed 1.0.
    calls_declared: Counter = Counter()
    call_results: Counter = Counter()
    timeouts = 0
    refusals = 0
    refusals_recovered = 0  # refusals that proposed an alternative (#1366)
    id_to_tool: Dict[str, str] = {}
    consec_tool = None
    consec_n = 0
    max_runs: Counter = Counter()  # tool -> max consecutive in this session

    for obj in messages:
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant":
            tcs = obj.get("tool_calls") or []
            names = []
            for tc in tcs:
                if isinstance(tc, dict) and tc.get("function"):
                    nm = tc["function"].get("name")
                    if nm:
                        names.append(nm)
                        calls_declared[nm] += 1
                        if tc.get("id"):
                            id_to_tool[tc["id"]] = nm
            # consecutive same-single-tool run tracking
            if len(set(names)) == 1:
                tool = names[0]
                if tool == consec_tool:
                    consec_n += 1
                else:
                    consec_tool, consec_n = tool, 1
                max_runs[consec_tool] = max(max_runs[consec_tool], consec_n)
            else:
                consec_tool, consec_n = None, 0
            content = obj.get("content")
            if isinstance(content, str) and _REFUSAL_RE.search(content):
                refusals += 1
                # Did the same turn also offer a way forward? (#1366)
                if _is_recovered_refusal(content):
                    refusals_recovered += 1
        elif role == "tool":
            content = obj.get("content")
            tool = id_to_tool.get(obj.get("tool_call_id"), "unknown")
            call_results[tool] += 1
            failed = _tool_result_failed(content)
            if failed:
                tool_failures[tool] += 1
                # Sub-classify the failure mode so the introspection loop can
                # attribute regressions to the actual cause (#1325) — a fix for
                # ``read_file:file-not-found`` should not be credited for
                # ``read_file:timeout``.
                reason = _classify_failure_reason(content)
                if reason is not None:
                    tool_failures_by_reason.setdefault(tool, {})
                    tool_failures_by_reason[tool][reason] = (
                        tool_failures_by_reason[tool].get(reason, 0) + 1
                    )
            if failed and isinstance(content, str) and _TIMEOUT_RE.search(content):
                timeouts += 1

    repeated = {t: n for t, n in max_runs.items() if n >= _REPEAT_THRESHOLD}
    tool_calls = {
        tool: max(calls_declared[tool], call_results[tool])
        for tool in set(calls_declared) | set(call_results)
    }
    return {
        "tool_failures": dict(tool_failures),
        "tool_failures_by_reason": tool_failures_by_reason,
        "tool_calls": tool_calls,
        "timeouts": timeouts,
        "refusals": refusals,
        "refusals_with_recovery": refusals_recovered,
        "repeated_tool_runs": repeated,
    }


def scan_session(path: Path) -> Dict[str, Any]:
    """Per-session signals from a JSONL transcript (one JSON object per line)."""
    return scan_messages(_iter_lines(path))


def _request_dump_messages(obj: Dict[str, Any]) -> List[Any]:
    """The conversation messages carried inside a request_dump_*.json snapshot
    live at request.body.messages — the same role-tagged shape as a JSONL
    transcript, so scan_messages handles it directly."""
    req = obj.get("request") if isinstance(obj, dict) else None
    body = req.get("body") if isinstance(req, dict) else None
    msgs = body.get("messages") if isinstance(body, dict) else None
    return msgs if isinstance(msgs, list) else []


def scan_request_dump(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Per-session signals from a request_dump_*.json snapshot (#238).

    Reuses scan_messages over request.body.messages, and adds the provider-layer
    error signal from the top-level ``error`` object — but ONLY its status code
    and error type, never ``message``/``body``/``response_text`` (those can echo
    private content; the no-raw-text contract still holds). Also records the
    model id used, which is anonymized metadata, not user content."""
    s = scan_messages(_request_dump_messages(obj))
    provider_errors: Counter = Counter()
    err = obj.get("error")
    if isinstance(err, dict):
        status = err.get("status_code") or err.get("response_status")
        # Prefer the structured recovery class (#236) over the raw exception type:
        # `rate_limit`/`auth`/`model_not_found` groups recurring bad provider-model
        # pairs far better than `RuntimeError`/`BadRequestError` (#237 pt3). Falls
        # back to the exception type for dumps written before failure_category.
        label = err.get("failure_category") or err.get("type") or "error"
        provider_errors[f"{status}:{label}" if status else str(label)] += 1
    s["provider_errors"] = dict(provider_errors)
    body = (
        obj.get("request", {}).get("body")
        if isinstance(obj.get("request"), dict)
        else None
    )
    model = body.get("model") if isinstance(body, dict) else None
    s["models"] = {model: 1} if isinstance(model, str) and model else {}
    return s


def _fresh(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def _assert_rate_bounds(
    failure_rate: Dict[str, float],
    failure_rate_by_reason: Dict[str, Dict[str, float]],
) -> None:
    """Hard-fail the digest if any published rate is outside [0, 1] (#317).

    A rate above 1.0 is not a curiosity — it means the denominator is wrong
    (the pre-#317 bug reported 0.70 for a tool failing 8% of the time). Failing
    loudly here keeps a bad denominator from silently mis-ranking the whole
    introspection stage, which is the input to every downstream priority score.
    """
    for tool, rate in failure_rate.items():
        if not 0.0 <= rate <= 1.0:
            raise ValueError(
                f"introspection digest rate out of bounds for tool {tool!r}: "
                f"{rate!r} (denominator is per-tool call count — #317)"
            )
    for tool, reasons in failure_rate_by_reason.items():
        for reason, rate in reasons.items():
            if not 0.0 <= rate <= 1.0:
                raise ValueError(
                    f"introspection digest reason rate out of bounds for "
                    f"{tool}:{reason}: {rate!r} (per-tool call denominator — #317)"
                )


def build_digest(
    sessions_dir: Path, window_days: int = 7, now: float | None = None
) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400
    failures: Counter = Counter()
    failures_by_reason: Dict[
        str, Dict[str, int]
    ] = {}  # tool -> reason -> count (#1325)
    calls: Counter = Counter()  # tool -> call count, the rate denominator (#317)
    timeouts = 0
    refusals = 0
    refusals_recovered = 0  # subset of refusals that offered a path forward (#1366)
    provider_errors: Counter = Counter()
    models: Counter = Counter()
    repeated: Dict[str, Dict[str, int]] = {}  # tool -> {max_consecutive, sessions}
    scanned = 0

    def _aggregate(s: Dict[str, Any]) -> None:
        nonlocal timeouts, refusals, refusals_recovered
        failures.update(s.get("tool_failures", {}))
        calls.update(s.get("tool_calls", {}))
        # Sub-classified failures (#1325) — merge per-tool reason counters so
        # the digest surfaces the dominant failure mode per tool, breaking the
        # fix→regress→refile treadmill where one mode's fix is blamed for another.
        for tool, reasons in s.get("tool_failures_by_reason", {}).items():
            if not isinstance(reasons, dict):
                continue
            bucket = failures_by_reason.setdefault(tool, {})
            for reason, n in reasons.items():
                bucket[reason] = bucket.get(reason, 0) + int(n)
        timeouts += s.get("timeouts", 0)
        refusals += s.get("refusals", 0)
        refusals_recovered += s.get("refusals_with_recovery", 0)
        provider_errors.update(s.get("provider_errors", {}))
        models.update(s.get("models", {}))
        for tool, n in s.get("repeated_tool_runs", {}).items():
            r = repeated.setdefault(tool, {"max_consecutive": 0, "sessions": 0})
            r["max_consecutive"] = max(r["max_consecutive"], n)
            r["sessions"] += 1

    if sessions_dir.is_dir():
        # 1. Native JSONL transcripts (the upstream session format).
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not _fresh(path, cutoff):
                continue
            scanned += 1
            _aggregate(scan_session(path))

        # 2. request_dump_*.json snapshots (#238 — this install persists sessions
        #    this way, so the JSONL glob found zero and the whole pipeline went
        #    blind). Multiple dumps of one session each carry a growing prefix of
        #    the same conversation, so dedup by session_id keeping the most
        #    complete snapshot — one session contributes its signals once.
        dumps: Dict[str, tuple] = {}  # session_id -> (msg_count, obj)
        for path in sorted(sessions_dir.glob("request_dump_*.json")):
            if not _fresh(path, cutoff):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    obj = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            sid = obj.get("session_id") or path.stem
            n_msgs = len(_request_dump_messages(obj))
            if n_msgs >= dumps.get(sid, (-1, None))[0]:
                dumps[sid] = (n_msgs, obj)
        for _sid, (_n, obj) in dumps.items():
            scanned += 1
            _aggregate(scan_request_dump(obj))

        # 3. SQLite SessionDB messages table (#399 / #623) — canonical store
        #    for real sessions.  Some installs keep state.db at
        #    HERMES_HOME/state.db rather than under sessions_dir, so probe both
        #    locations and de-duplicate by resolved path.
        seen_db_paths: set[str] = set()
        for db_path in (
            sessions_dir / "state.db",
            Path(os.environ.get("HERMES_HOME", str(get_hermes_home()))) / "state.db",
            get_hermes_home() / "state.db",
        ):
            try:
                resolved = db_path.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen_db_paths:
                continue
            seen_db_paths.add(key)
            if db_path.is_file():
                for _sid, msgs in _iter_state_db(db_path):
                    if not msgs:
                        continue
                    # Issue #3145: Filter by --days cutoff so 7-day digest
                    # doesn't aggregate all historical SessionDB sessions.
                    # A session is fresh if any message timestamp >= cutoff,
                    # or if no messages carry timestamps, fall back to file mtime.
                    has_ts = any("timestamp" in m for m in msgs)
                    if has_ts:
                        if not any(m.get("timestamp", 0) >= cutoff for m in msgs):
                            continue
                    elif not _fresh(db_path, cutoff):
                        continue
                    scanned += 1
                    _aggregate(_state_db_session_signals(msgs))

    # Per-call failure rate (#317). The denominator is the number of CALLS to
    # that tool, not the number of sessions scanned: a tool that is called 20
    # times in one session used to report 20 / sessions, which exceeded 1.0 on
    # real data (terminal 0.70, ha_get_state 0.62 against true 8.0% / 9.7%) and
    # inflated every downstream priority score. A rate is only quotable next to
    # its base, so ``tool_calls`` is published alongside ``tool_failures``.
    tool_failures = dict(failures.most_common())
    tool_calls = dict(sorted(calls.items()))
    failure_rate = {
        tool: (count / tool_calls[tool] if tool_calls.get(tool) else 0.0)
        for tool, count in tool_failures.items()
    }
    # Reason-scoped rate (issue #1325 — corrected fix). PR #1336 added reason
    # *counts* (tool_failures_by_reason) but the realized-impact gate still
    # compared the aggregate tool rate, so a fix that eliminated one reason
    # (e.g. read_file:file-not-found) could not register against a tool rate
    # dominated by an UNAFFECTED reason (read_file:timeout) → "no-signal".
    # Exposing the per-(tool,reason) rate gives the verification loop a target
    # that moves only when the targeted reason moves, breaking the treadmill.
    # Same denominator as above — per tool CALL, not per session (#317).
    failure_rate_by_reason: Dict[str, Dict[str, float]] = {}
    for tool, reasons in failures_by_reason.items():
        failure_rate_by_reason[tool] = {
            reason: (n / tool_calls[tool] if tool_calls.get(tool) else 0.0)
            for reason, n in reasons.items()
        }
    _assert_rate_bounds(failure_rate, failure_rate_by_reason)
    spiral_depth_per_session = {
        tool: round((meta["max_consecutive"] / scanned if scanned else 0.0), 4)
        for tool, meta in repeated.items()
    }
    # Share of refusals that offered a way forward instead of stopping (#1366).
    # Denominator is refusals, NOT sessions: this measures the quality of the
    # refusals that happened, not how often the agent refuses. Both numbers
    # matter and they move independently — refusals can fall while the recovery
    # rate holds, or rise while recovery improves.
    refusal_recovery_rate = round(refusals_recovered / refusals, 4) if refusals else 0.0

    return {
        "window_days": window_days,
        "sessions_scanned": scanned,
        # Which denominator the published rates use (#317). Stored in the
        # realized-impact ledger so a baseline recorded under one basis is
        # never compared against a current rate computed under another.
        "failure_rate_basis": "per_tool_call",
        "signals": {
            "tool_failures": tool_failures,
            "tool_failures_by_reason": failures_by_reason,
            # Denominator of every published rate (#317). A rate must never be
            # quoted without its base, so both are emitted together.
            "tool_calls": tool_calls,
            "failure_rate": failure_rate,
            "failure_rate_by_reason": failure_rate_by_reason,
            "spiral_depth_per_session": spiral_depth_per_session,
            "timeouts": timeouts,
            "refusals_or_access_denied": refusals,
            "refusals_with_recovery": refusals_recovered,
            "refusal_recovery_rate": refusal_recovery_rate,
            "repeated_tool_runs": repeated,
            "provider_errors": dict(provider_errors.most_common()),
            "models_used": dict(models.most_common()),
        },
    }


def _sessions_dir() -> Path:
    return get_hermes_home() / "sessions"


def main(argv: List[str]) -> int:
    days = 7
    for a in argv[1:]:
        if a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
    digest = build_digest(_sessions_dir(), window_days=days)
    print(json.dumps(digest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
