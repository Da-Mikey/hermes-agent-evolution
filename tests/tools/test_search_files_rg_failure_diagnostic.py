"""#330: a failed file-name search must say WHY, not just that it failed.

The ``rg --files`` transport could not merge stderr into stdout the way the
content-search path does — that stream is parsed as filenames — so it discarded
stderr instead. The consequence was the bare string "File search failed while
running ripgrep.": no exit code, no diagnostic, which the failure taxonomy then
treated as a dead end. The model could not distinguish a bad glob from an
unreadable root from a broken binary, and had no reason to believe a corrected
retry would work.

The fix keeps ONE rg invocation (a failed search must not re-run the scan, an
invariant pinned by test_search_files_engine_selection) and captures stderr
alongside stdout on its own pipe. These tests pin the observable contract.
"""

import shutil

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_operations_common import ExecuteResult


def _operations(result):
    """A searcher whose rg transport returns *result* and records each call."""
    operations = ShellFileOperations(LocalEnvironment())
    operations._resolve_command = lambda command: "/usr/bin/rg" if command == "rg" else None
    calls = []

    def fake_run_rg_bounded(words, fetch_limit, timeout, *, merge_stderr=False,
                            native_ok=True, shell_prefix=""):
        calls.append({"words": words, "merge_stderr": merge_stderr})
        return result

    operations._run_rg_bounded = fake_run_rg_bounded
    operations.calls = calls
    return operations


def test_failed_search_surfaces_exit_code_and_stderr(tmp_path):
    operations = _operations(ExecuteResult(
        stdout="", exit_code=2,
        stderr="rg: /definitely/not/here: No such file or directory (os error 2)\n"))

    result = operations._search_files_rg("*.py", str(tmp_path), limit=10, offset=0)

    assert result.error, "a failing search must not return files"
    # The status is always reported, so the caller can classify the failure.
    assert "exit code 2" in result.error
    # ...and ripgrep's own words are visible instead of an opaque placeholder.
    assert "No such file or directory" in result.error
    # The old content-free message is gone.
    assert result.error.strip() != "File search failed while running ripgrep."
    # A failed search is NOT re-run: exactly one invocation, always.
    assert len(operations.calls) == 1


def test_stderr_is_not_merged_into_the_filename_stream(tmp_path):
    """The scan must never run with merge_stderr: stdout is parsed as filenames."""
    operations = _operations(ExecuteResult(stdout="", exit_code=2, stderr="boom"))

    operations._search_files_rg("*.py", str(tmp_path), limit=10, offset=0)

    assert operations.calls[0]["merge_stderr"] is False


def test_failed_search_without_diagnostics_still_says_what_to_do(tmp_path):
    """A silent failure must not be a dead end either."""
    operations = _operations(ExecuteResult(stdout="", exit_code=2, stderr=""))

    result = operations._search_files_rg("*.py", str(tmp_path), limit=10, offset=0)

    assert "exit code 2" in result.error
    assert "no diagnostics" in result.error
    # Actionable: says a corrected retry is the way forward.
    assert "retry" in result.error.lower()


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_native_transport_captures_stderr_on_a_real_failure(tmp_path):
    """End-to-end: the native transport really populates ExecuteResult.stderr.

    Searching a path that does not exist makes rg exit non-zero with a message on
    stderr. Before #330 that message was discarded (``stderr=DEVNULL``).
    """
    operations = ShellFileOperations(LocalEnvironment())
    missing = str(tmp_path / "does-not-exist")

    result = operations._run_rg_native([f"rg --files -- '{missing}'"], 10, timeout=30)

    assert result.exit_code not in {0, 1}, "searching a missing path must fail"
    assert "does-not-exist" in (result.stderr or ""), (
        f"expected rg's stderr to be captured, got {result.stderr!r}")
