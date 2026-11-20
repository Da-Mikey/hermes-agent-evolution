"""Another user's home is a sensitive write target too (#424).

`tools/approval_detection.py` normalizes a command by folding the *invoking* user's home back to
``~/`` (``_rewrite_resolved_user_home`` → ``_fold_home_prefixes``), which is why ``~/.ssh``,
``$HOME/.ssh`` and ``/home/mike/.ssh`` all hit the same rule. Nothing folded a *different* user's
home, so before this change every write door onto ``/root/.ssh/authorized_keys`` — cp, mv, install,
tee, ``>``, ``>>``, ``sed -i``, ``sort -o``, ``truncate`` — returned ``(False, None, None)``: no
finding, no reason string, no denial event. Measured on the base tree: 72/72 commands of the
door×target matrix were clean; the same 72 against ``~/`` were flagged.

Prometheus runs as ``mike`` with a narrow ``sudo -n`` helper, so a sudo-prefixed command naming a
``/root`` path *is* reachable from an agent shell. ``~/.ssh`` and ``/root/.ssh`` both hold keys that
grant login; only one of them used to prompt.

The gate is still the TARGET, never the prefix: an ordinary file under another user's home must
stay clean, otherwise the rule is noise and gets switched off.
"""

import re

import pytest

from tools.approval_detection import (
    _CREDENTIAL_FILES,
    _SHELL_RC_FILES,
    _SSH_SENSITIVE_PATH,
    detect_dangerous_command,
)

# Other users' homes, written literally — what home folding leaves untouched.
OTHER_USER_TARGETS = [
    "/root/.ssh/authorized_keys",
    "/root/.ssh/config",
    "/root/.ssh/id_rsa",
    "/home/bob/.ssh/authorized_keys",
    "/root/.bashrc",
    "/root/.profile",
    "/home/bob/.zshrc",
    "/root/.netrc",
    "/root/.pgpass",
    "/root/.aws/credentials",
    "/home/bob/.aws/credentials",
]

# Every binary/operator that names its own write destination.
WRITE_DOORS = [
    "cp /tmp/payload {target}",
    "mv /tmp/payload {target}",
    "install -m 600 /tmp/payload {target}",
    "tee {target}",
    "tee -a {target}",
    "echo pwned > {target}",
    "echo pwned >> {target}",
    "printf pwned > {target}",
    "sed -i 's/^/#/' {target}",
    "sort -o {target}",
    "truncate -s 0 {target}",
    "uniq /tmp/payload {target}",
]


@pytest.mark.parametrize("target", OTHER_USER_TARGETS)
@pytest.mark.parametrize("door", WRITE_DOORS)
def test_write_door_onto_another_users_home_is_flagged(door, target):
    """No write door may reach a credential path under another user's home unflagged."""
    command = door.format(target=target)
    result = detect_dangerous_command(command)
    assert result[0] is True, f"{command!r} was certified as an ordinary command"
    # A flag with no reason is invisible in the audit trail — the defect this closes.
    assert result[1], f"{command!r} flagged with no reason string"


@pytest.mark.parametrize(
    "command",
    [
        # The form this host can actually reach: sudo-prefixed, literal /root path.
        "sudo cp /tmp/payload /root/.ssh/authorized_keys",
        "sudo -n tee /root/.ssh/authorized_keys",
        "sudo sh -c 'echo pwned >> /root/.ssh/authorized_keys'",
        # Quoted spellings survive normalization and must still match.
        'cp /tmp/payload "/root/.ssh/authorized_keys"',
        "cp /tmp/payload '/home/bob/.ssh/authorized_keys'",
        # Credential files other than ssh keys.
        "sudo cp /tmp/payload /root/.aws/credentials",
    ],
)
def test_realistic_other_user_home_spellings_are_flagged(command):
    assert detect_dangerous_command(command)[0] is True, command


@pytest.mark.parametrize(
    "target",
    [
        "~/.ssh/authorized_keys",
        "$HOME/.ssh/authorized_keys",
        "${HOME}/.ssh/authorized_keys",
        "~/.bashrc",
        "~/.netrc",
        "~/.aws/credentials",
    ],
)
def test_invoking_users_home_still_flagged_by_folding(target):
    """The existing fold-to-``~/`` path must keep working — this change only adds a second source."""
    assert detect_dangerous_command(f"cp /tmp/payload {target}")[0] is True, target


@pytest.mark.parametrize(
    "command",
    [
        # Ordinary files under another user's home: the rule must not become a blanket magnet.
        "cp /tmp/report /root/notes.txt",
        "echo done > /home/bob/notes.txt",
        "sed -i 's/a/b/' /home/bob/notes.txt",
        "cp /tmp/report /home/mike/notes.txt",
        # Reads of the same paths are unchanged by this rule set.
        "ls /root/.ssh",
        "cat /root/.ssh/config",
        "grep -rn PermitRootLogin /root/.ssh/config",
        # A longer path that merely ENDS in a /root component is not a root home.
        "cp /tmp/report /home/mike/project/root/.ssh/notes.md",
        # src position in cp is a read, not a write destination.
        "cp /root/.ssh/config /tmp/copy",
        "cp /home/bob/.bashrc /tmp/copy",
    ],
)
def test_neutral_targets_under_another_users_home_stay_clean(command):
    result = detect_dangerous_command(command)
    assert result[0] is False, f"{command!r} flagged ({result[1]}) — rule is too broad"


def test_other_user_path_fragments_are_expressed_in_the_target_sets():
    """Structural guard: the three target families each cover a literal other-user home.

    The defect was a scope assumption (``only my home has credentials``), not a typo — so assert
    the scope itself, or the next edit can silently drop one family back to ``~/`` only.
    """
    samples = {
        "_SSH_SENSITIVE_PATH": "/root/.ssh/authorized_keys",
        "_SHELL_RC_FILES": "/root/.bashrc",
        "_CREDENTIAL_FILES": "/root/.netrc",
    }
    for name, pattern in (
        ("_SSH_SENSITIVE_PATH", _SSH_SENSITIVE_PATH),
        ("_SHELL_RC_FILES", _SHELL_RC_FILES),
        ("_CREDENTIAL_FILES", _CREDENTIAL_FILES),
    ):
        compiled = re.compile(pattern)
        assert "/root" in pattern, f"{name} does not know about /root"
        assert "/home/" in pattern, f"{name} does not know about /home/<user>"
        sample = samples[name]
        assert compiled.search(sample), f"{name} does not match {sample}"
        assert compiled.search(sample.replace("/root", "/home/bob")), (
            f"{name} does not match another user's home"
        )
        # Matched at a path boundary: a longer component ending in /root is not a root home.
        assert not compiled.search("/srv/jail" + sample), (
            f"{name} matches an embedded /root component — over-broad"
        )
