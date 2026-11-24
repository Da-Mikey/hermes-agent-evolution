"""A named profile's config.yaml is the same approval policy file as the root one.

``~/.hermes/profiles/<name>/config.yaml`` holds its own ``approvals.mode`` /
``yolo`` / ``allowlist``, and the config cache is mtime-keyed, so an in-place
edit takes effect mid-session. The sensitive-target pattern knew only the root
``~/.hermes/config.yaml``, so every profile's policy file was rewritten without
an approval prompt.

Upstream covers both under one home prefix (``profiles/<name>/`` optional);
these cases mirror that contract, including the sibling that must stay safe so
the fix cannot pass by over-gating everything.
"""

import pytest

from tools.approval import detect_dangerous_command

_USER_HOME = "/home/mike"


@pytest.fixture
def hermes_home(monkeypatch):
    monkeypatch.setenv("HOME", _USER_HOME)
    monkeypatch.setenv("HERMES_HOME", _USER_HOME + "/.hermes")
    for key in ("USERPROFILE", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("command", [
    # the policy file of a named profile, in every spelling a shell uses
    "sed -i s/a/b/ ~/.hermes/profiles/devyn/config.yaml",
    "sed -i s/a/b/ $HOME/.hermes/profiles/hestia/config.yaml",
    "sed -i s/a/b/ ${HOME}/.hermes/profiles/iris/config.yaml",
    f"sed -i s/a/b/ {_USER_HOME}/.hermes/profiles/athena/config.yaml",
    "cat k | tee -a ~/.hermes/profiles/devyn/config.yaml",
    "echo x >> ~/.hermes/profiles/hearth/config.yaml",
])
def test_a_profiles_policy_file_is_gated(hermes_home, command):
    assert detect_dangerous_command(command)[0], command


@pytest.mark.parametrize("command", [
    # the root policy file keeps its gate
    "sed -i s/a/b/ ~/.hermes/config.yaml",
    "echo x >> ~/.hermes/.env",
])
def test_the_root_policy_file_stays_gated(hermes_home, command):
    assert detect_dangerous_command(command)[0], command


@pytest.mark.parametrize("command", [
    # a sibling whose name merely starts with "profiles" is not a profile home
    "sed -i s/a/b/ ~/.hermes/profiles-old/config.yaml",
    # ordinary files stay out of the gate
    "sed -i s/a/b/ ~/notes.txt",
])
def test_a_sibling_is_not_a_profile_home(hermes_home, command):
    assert not detect_dangerous_command(command)[0], command
