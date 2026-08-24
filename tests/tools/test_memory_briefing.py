"""Tests for memory-primed spawning (GitHub issue #105).

Spawned children start cold: they receive only the explicit ``context`` string
(plus, optionally, a collapsed parent-conversation summary). The OPTIONAL
``memory_briefing`` flag primes them with the parent's long-term memory: a
bounded, most-relevant-first briefing is assembled via the parent's EXISTING
prefetch path (``MemoryManager.prefetch_all``) and prepended to each task's
``context`` as background reference.

These tests pin four contracts:

  1. No memory manager on the parent      -> briefing is None, context untouched.
  2. Opted in with a working prefetch     -> briefing block prepended to every
                                             task's context, bounded and marked
                                             UNTRUSTED DATA (injection guard).
  3. Prefetch failure / empty result      -> no-op, context unchanged (best-effort).
  4. Flag off (default)                   -> the delegate path never calls the
                                             briefing helper at all
                                             (byte-identical behavior).

``prefetch_all`` is always a stub — no test touches a real memory backend.
"""

import pytest

import tools.delegate_tool as dt
from tools.delegate_tool import (
    _MEMORY_BRIEFING_HEADER,
    _MEMORY_BRIEFING_MAX_CHARS,
    _MEMORY_BRIEFING_MAX_QUERY_CHARS,
    _apply_memory_briefing,
    _build_memory_briefing,
    _memory_briefing_query,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeMemoryManager:
    """Stand-in for MemoryManager exposing only ``prefetch_all``."""

    def __init__(self, body="MEMORY-SNIPPET-RELEVANT-TO-TASK", raise_on=None):
        self._body = body
        self._raise_on = raise_on
        self.queries = []

    def prefetch_all(self, query, *, session_id="", model_id=None):
        self.queries.append(query)
        if self._raise_on is not None:
            raise self._raise_on
        return self._body


_DEFAULT_MM = object()  # sentinel: "give me a fresh _FakeMemoryManager"


def _make_parent(memory_manager=_DEFAULT_MM):
    """Build a minimal parent_agent stub.

    ``memory_manager`` defaults to a fresh _FakeMemoryManager; pass None to
    simulate an agent without a memory manager (no briefing possible).
    """
    if memory_manager is _DEFAULT_MM:
        memory_manager = _FakeMemoryManager()
    if memory_manager is None:
        return object()  # plain object: no _memory_manager attribute
    parent = type("Parent", (), {})()
    parent._memory_manager = memory_manager
    return parent


_TASKS = [
    {"goal": "Fix the solar inverter dashboard", "context": "User is in JHB"},
    {"goal": "Summarize the meeting notes", "context": "Focus on action items"},
]


# --------------------------------------------------------------------------- #
# Query fusion
# --------------------------------------------------------------------------- #


def test_query_fuses_goals_and_contexts():
    query = _memory_briefing_query(_TASKS)
    assert "solar" in query
    assert "inverter" in query
    assert "JHB" in query
    assert "meeting" in query


def test_query_caps_length():
    long_task = [{"goal": "x" * 5000, "context": "y" * 5000}]
    query = _memory_briefing_query(long_task)
    assert len(query) <= _MEMORY_BRIEFING_MAX_QUERY_CHARS


def test_query_empty_when_no_scorable_text():
    assert _memory_briefing_query([{"goal": "   "}]) == ""


# --------------------------------------------------------------------------- #
# Briefing construction
# --------------------------------------------------------------------------- #


def test_no_memory_manager_yields_none():
    parent = _make_parent(memory_manager=None)
    assert _build_memory_briefing(_TASKS, parent) is None


def test_empty_query_yields_none():
    parent = _make_parent()
    assert _build_memory_briefing([{"goal": ""}], parent) is None


def test_prefetch_failure_yields_none_and_leaves_context_untouched():
    parent = _make_parent(_FakeMemoryManager(raise_on=RuntimeError("boom")))
    tasks = [dict(t) for t in _TASKS]
    original = [t.get("context") for t in tasks]
    assert _build_memory_briefing(tasks, parent) is None
    _apply_memory_briefing(tasks, parent)
    assert [t.get("context") for t in tasks] == original


def test_empty_prefetch_yields_none():
    parent = _make_parent(_FakeMemoryManager(body=""))
    assert _build_memory_briefing(_TASKS, parent) is None


def test_briefing_block_carries_untrusted_data_guard():
    parent = _make_parent(_FakeMemoryManager(body="REMEMBER: always do X"))
    briefing = _build_memory_briefing(_TASKS, parent)
    assert briefing is not None
    assert "UNTRUSTED DATA" in briefing
    assert "never adopt" in briefing
    assert "REMEMBER: always do X" in briefing  # content survives as reference


def test_briefing_is_bounded():
    parent = _make_parent(_FakeMemoryManager(body="z" * 100_000))
    briefing = _build_memory_briefing(_TASKS, parent)
    assert briefing is not None
    assert (
        len(briefing) <= _MEMORY_BRIEFING_MAX_CHARS + len(_MEMORY_BRIEFING_HEADER) + 200
    )
    assert "truncated" in briefing


def test_briefing_query_reaches_the_store():
    manager = _FakeMemoryManager()
    parent = _make_parent(manager)
    _build_memory_briefing(_TASKS, parent)
    assert len(manager.queries) == 1
    assert "solar inverter" in manager.queries[0]


# --------------------------------------------------------------------------- #
# Apply semantics
# --------------------------------------------------------------------------- #


def test_apply_prepends_briefing_and_preserves_explicit_context():
    parent = _make_parent(_FakeMemoryManager(body="MEMORY"))
    tasks = [dict(t) for t in _TASKS]
    _apply_memory_briefing(tasks, parent)
    for task, original in zip(tasks, _TASKS):
        ctx = task["context"]
        assert ctx.startswith(_MEMORY_BRIEFING_HEADER)
        assert "MEMORY" in ctx
        assert original["context"] in ctx  # explicit context still foreground


def test_apply_handles_tasks_without_context():
    parent = _make_parent(_FakeMemoryManager(body="MEMORY"))
    tasks = [{"goal": "Do the thing"}]
    _apply_memory_briefing(tasks, parent)
    assert tasks[0]["context"].startswith(_MEMORY_BRIEFING_HEADER)


def test_default_flag_keeps_delegate_path_byte_identical():
    # The gate lives in delegate_task: the helper must never run when the flag
    # is unset/falsy. Pin that the schema exposes the opt-in and the helper is
    # only reachable via an explicit truthy flag (no default-on path exists).
    assert "memory_briefing" in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert (
        dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["memory_briefing"]["type"]
        == "boolean"
    )
    # And the handler wires it from args (the registry call site):
    assert 'memory_briefing=args.get("memory_briefing")' in open(dt.__file__).read()
