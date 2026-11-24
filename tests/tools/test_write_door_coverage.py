"""Coverage for write doors the approval scanner used to certify as reads (#409).

`tools/approval_detection.py` gates the write doors it enumerates (redirection, tee,
cp/mv/install, dd, sed -i) but four other binaries write a file they name and returned
``(False, None, None)`` — no reason, no denial event, nothing for the audit trail:

    sort -o / --output      GNU sort writes its argument when -o is given
    truncate                truncates every operand it names
    ln -s / -f              replaces its destination
    uniq IN OUT             writes the second operand (the OUTPUT form)

The gate is the TARGET, never the option: neutral destinations must stay clean, otherwise
every `sort -o` in ordinary use would demand approval and the rule would be turned off.

The last tests are the structural guard that would have caught this defect — the module
already modelled `sort`'s option tables and never asked what `-o` does with its argument.
"""

import pytest

from tools.approval_detection import (
    DANGEROUS_PATTERNS,
    _READ_TOOL_EXEC_FLAGS,
    _READ_TOOL_LONG_OPTIONS_WITH_ARG,
    _READ_TOOL_NOT_A_WRITER,
    _READ_TOOL_SHORT_OPTIONS_WITH_ARG,
    _WRITE_DOOR_BINARIES,
    detect_dangerous_command,
)

# Each entry writes a sensitive target and must produce a finding.
SENSITIVE_WRITES = [
    "sort -o ~/.ssh/authorized_keys /tmp/x",
    "sort --output=$HOME/.ssh/authorized_keys /tmp/x",
    "sort -o .env /tmp/x",
    "sort -o /etc/hosts /tmp/x",
    "truncate -s 0 ~/.ssh/authorized_keys",
    "ln -sf /tmp/x ~/.ssh/authorized_keys",
    "uniq /tmp/x ~/.ssh/authorized_keys",
]

# The obvious over-block to avoid: the TOOL is not dangerous, the TARGET is.
NEUTRAL_WRITES = [
    "sort -o /tmp/out.txt /tmp/in.txt",
    "sort --output=/tmp/out.txt /tmp/in.txt",
    "uniq /tmp/in.txt /tmp/out.txt",
    "truncate -s 0 /tmp/out.txt",
    "ln -sf /tmp/x /tmp/y",
    "ln -s /tmp/x /tmp/link",
    # Reading OUT of a sensitive path is not a write door.
    "sort ~/.ssh/authorized_keys",
    "uniq ~/.ssh/authorized_keys",
    "cat ~/.ssh/authorized_keys",
]

# One probe per declared door so a door cannot be declared and left unmatched.
WRITE_DOOR_PROBES = {
    "sort": "sort -o ~/.ssh/authorized_keys /tmp/x",
    "truncate": "truncate -s 0 ~/.ssh/authorized_keys",
    "ln": "ln -sf /tmp/x ~/.ssh/authorized_keys",
    "uniq": "uniq /tmp/x ~/.ssh/authorized_keys",
}


@pytest.mark.parametrize("cmd", SENSITIVE_WRITES)
def test_unpaired_write_doors_are_gated(cmd):
    is_dangerous, key, description = detect_dangerous_command(cmd)
    assert is_dangerous is True, f"{cmd} writes a sensitive path and was certified safe"
    assert key, "a gated command must carry a pattern key"
    assert description, "a gated command must carry a reason"


@pytest.mark.parametrize("cmd", SENSITIVE_WRITES)
def test_write_door_reason_names_the_binary(cmd):
    """A refusal must stay greppable by pattern key, so the reason names the door."""
    binary = cmd.split()[0]
    is_dangerous, key, description = detect_dangerous_command(cmd)
    assert is_dangerous is True
    assert binary in description.lower(), (
        f"reason {description!r} does not name the binary {binary!r} that opened the door"
    )
    assert key == description, "pattern key and description are the same field here"


@pytest.mark.parametrize("cmd", NEUTRAL_WRITES)
def test_neutral_targets_stay_clean(cmd):
    is_dangerous, key, description = detect_dangerous_command(cmd)
    assert is_dangerous is False, (
        f"{cmd} is ordinary use but was gated as {description!r}"
    )
    assert key is None and description is None


@pytest.mark.parametrize(
    "cmd",
    [
        # Fixtures from test_execution_flag_detection.py:106,110 — the output option owns its
        # argument, so --compress-program there is DATA and must not start prompting.
        "sort --output --compress-program names.txt",
        "sort -o --compress-program names.txt",
        "sort -- --compress-program sh",
    ],
)
def test_output_option_ownership_not_regressed(cmd):
    """A new write-door rule must not widen these existing ownership fixtures."""
    assert detect_dangerous_command(cmd) == (False, None, None)


@pytest.mark.parametrize("binary", sorted(WRITE_DOOR_PROBES))
def test_write_door_fires_through_the_public_classifier(binary):
    """Every declared door must actually fire from the function the runtime calls."""
    probe = WRITE_DOOR_PROBES[binary]
    is_dangerous, key, _ = detect_dangerous_command(probe)
    assert is_dangerous is True, (
        f"{binary} is declared a write door but {probe!r} was certified safe"
    )
    assert key and binary in key.lower(), (
        f"{binary} door fires under an unclear key {key!r}"
    )


def test_write_door_probes_cover_the_declared_set():
    """A declared door with no probe would silently skip the check above."""
    assert set(WRITE_DOOR_PROBES) == set(_WRITE_DOOR_BINARIES)


def test_every_read_tool_is_classified_as_writer_or_signed_off():
    """Structural guard: a read-tool binary cannot be silently left unclassified.

    The module teaches its parser the argument grammar of these binaries; one whose
    argument-taking option names a write destination must also carry a write-door rule,
    or be signed off below as genuinely read-only. This is the check that would have
    caught #409 when `sort` was added to the option tables.
    """
    read_tools = (
        set(_READ_TOOL_EXEC_FLAGS)
        | set(_READ_TOOL_LONG_OPTIONS_WITH_ARG)
        | set(_READ_TOOL_SHORT_OPTIONS_WITH_ARG)
    )
    unclassified = read_tools - _WRITE_DOOR_BINARIES - _READ_TOOL_NOT_A_WRITER
    assert not unclassified, (
        f"{sorted(unclassified)} are modelled as read tools with argument-taking options "
        "but are neither gated as a write door nor signed off as not-a-writer"
    )


def test_signed_off_read_tools_are_actually_modelled():
    """The sign-off set may not drift away from the tables it excuses."""
    read_tools = (
        set(_READ_TOOL_EXEC_FLAGS)
        | set(_READ_TOOL_LONG_OPTIONS_WITH_ARG)
        | set(_READ_TOOL_SHORT_OPTIONS_WITH_ARG)
    )
    stale = _READ_TOOL_NOT_A_WRITER - read_tools
    assert not stale, f"{sorted(stale)} are signed off but not modelled as read tools"
    assert not (_WRITE_DOOR_BINARIES & _READ_TOOL_NOT_A_WRITER), (
        "a binary cannot be both"
    )


def test_every_write_door_binary_is_mentioned_by_a_pattern():
    """The declared set and the actual rules may not diverge."""
    sources = [pattern for pattern, _ in DANGEROUS_PATTERNS]
    for binary in sorted(_WRITE_DOOR_BINARIES):
        assert any(f"\\b{binary}\\b" in pattern for pattern in sources), (
            f"{binary} is declared a write door but no DANGEROUS_PATTERNS rule mentions it"
        )
