"""Read Tirith policy once without treating an unreadable policy as disabled."""
from __future__ import annotations


class TirithConfigurationError(ValueError):
    """The scanner policy could not be established safely."""


def configuration_error_result() -> dict:
    return {
        "action": "block", "configuration_error": True,
        "failure_class": "configuration_error", "findings": [],
        "summary": "Tirith security configuration could not be read. Repair the active config with "
                   "'hermes config edit' and 'hermes config check', then retry. No command was approved.",
    }


def read_security_policy() -> dict:
    try:
        from hermes_cli.config_read_errors import FailedConfigRead
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        raise TirithConfigurationError("Security configuration unavailable") from None
    if isinstance(config, FailedConfigRead) or not isinstance(config, dict):
        raise TirithConfigurationError("Security configuration unavailable")
    security = config.get("security", {})
    if security is None:
        security = {}
    if not isinstance(security, dict):
        raise TirithConfigurationError("Security configuration must be a mapping")

    return security
