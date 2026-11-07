"""#364: ``rg`` exit 2 with a usable listing is a PARTIAL success, not a failure.

ripgrep returns 2 when ONE path in the walk is unreadable, but it still writes
every filename it could read to stdout. Before this fix the filename path threw
that listing away and returned ``permission_denied`` instead, so a single
unreadable file turned ten real paths into a dead end.

The negative fixture (``test_empty_listing_with_error_still_reports_the_error``)
is the other half of the contract: when nothing usable survived, the error must
still be reported. A suite that only proved the listing survives would pass on a
change that swallowed every error.
"""

from types import SimpleNamespace

from tools.file_operations import SearchResult, ShellFileOperations


class FakeEnvironment:
    """Minimal non-local backend: the rg call itself is stubbed."""

    is_local = False
    cwd = "/workspace"


def _rg_result(*, stdout, stderr="", exit_code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


def _operations(result):
    operations = ShellFileOperations(FakeEnvironment())
    operations._resolve_command = lambda command: (
        "/usr/bin/rg" if command == "rg" else None
    )

    def _run_bounded(commands, fetch_limit, **kwargs):
        return result

    operations._run_rg_bounded = _run_bounded
    return operations


LISTING = "".join(f"/repo/src/mod{i}.py\n" for i in range(10))


def test_unreadable_file_does_not_discard_the_listing():
    """One unreadable path among readable ones keeps all ten filenames."""
    result = _operations(
        _rg_result(
            stdout=LISTING,
            stderr="rg: /repo/secret: Permission denied (os error 13)",
            exit_code=2,
        )
    )._search_files_rg("*.py", "/repo", 50, 0)

    assert result.error is None, f"listing was discarded: {result.error}"
    assert result.total_count == 10
    assert result.files[:2] == ["/repo/src/mod0.py", "/repo/src/mod1.py"]
    # The warning must name the cause, not just that something went wrong.
    assert "Permission denied" in result.warning
    assert "exit code 2" in result.warning


def test_empty_listing_with_error_still_reports_the_error():
    """Negative fixture: no usable payload means the failure still surfaces."""
    result = _operations(
        _rg_result(
            stdout="",
            stderr="rg: /repo: Permission denied (os error 13)",
            exit_code=2,
        )
    )._search_files_rg("*.py", "/repo", 50, 0)

    assert result.error is not None
    assert "Permission denied" in result.error
    assert result.files == []
    assert result.total_count == 0


def test_clean_exit_carries_no_warning():
    """A normal walk is untouched by the partial-failure path."""
    result = _operations(_rg_result(stdout=LISTING, exit_code=0))._search_files_rg(
        "*.py", "/repo", 50, 0
    )

    assert result.error is None
    assert result.total_count == 10
    assert result.warning is None


def test_partial_listing_still_respects_limit_and_offset():
    """Paginated reads stay correct when the walk was partial."""
    result = _operations(
        _rg_result(stdout=LISTING, stderr="rg: boom", exit_code=2)
    )._search_files_rg("*.py", "/repo", 3, 2)

    assert result.error is None
    assert result.files == [
        "/repo/src/mod2.py",
        "/repo/src/mod3.py",
        "/repo/src/mod4.py",
    ]
    assert result.total_count == 10
    assert result.truncated is True
