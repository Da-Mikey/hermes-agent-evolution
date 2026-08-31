#!/usr/bin/env python3
"""
Evolution Mode Detection for Hermes Evolution Agent

Detects whether the agent is running in PRIVATE, PUBLIC, or OFF mode.

Council 2026-08-31: "no token" is OFF, not PUBLIC. PUBLIC previously claimed
``create_issues: true`` with an empty token — the wake-gate was the only
backstop. The detector must not lie about GitHub permissions.
"""

import os
import sys
from typing import Literal

Mode = Literal["PUBLIC", "PRIVATE", "OFF"]


def detect_mode() -> Mode:
    """Detect the current mode from available tokens.

    Returns:
        "PRIVATE" if GITHUB_PRIVATE_TOKEN is set (owner merge mode)
        "PUBLIC" if only GITHUB_TOKEN is set (write issues/PRs, no merge)
        "OFF" if no GitHub token is configured (no GitHub outlet)
    """
    if os.getenv("GITHUB_PRIVATE_TOKEN"):
        return "PRIVATE"

    if os.getenv("GITHUB_TOKEN"):
        return "PUBLIC"

    return "OFF"


def require_private_mode() -> None:
    """Raise an exception if not in PRIVATE mode."""
    if detect_mode() != "PRIVATE":
        raise PermissionError(
            f"This operation requires PRIVATE mode. Current mode: {detect_mode()}"
        )


def get_github_token() -> str:
    """Get the appropriate GitHub token for the current mode."""
    mode = detect_mode()

    if mode == "PRIVATE":
        token = os.getenv("GITHUB_PRIVATE_TOKEN")
        if not token:
            raise ValueError("GITHUB_PRIVATE_TOKEN not set in PRIVATE mode")
        return token
    if mode == "PUBLIC":
        return os.getenv("GITHUB_TOKEN", "")
    return ""


def get_github_config() -> dict:
    """Get GitHub configuration based on mode."""
    mode = detect_mode()
    can_write = mode in ("PUBLIC", "PRIVATE")

    return {
        "mode": mode,
        "owner": "Lexus2016",
        "repo": "hermes-agent-evolution",
        "token_env": "GITHUB_PRIVATE_TOKEN" if mode == "PRIVATE" else "GITHUB_TOKEN",
        "permissions": {
            "read": can_write,
            "create_issues": can_write,
            "create_prs": can_write,
            "merge": mode == "PRIVATE",
            "modify_code": mode == "PRIVATE",
        },
    }


def main() -> int:
    """CLI to check current mode."""
    mode = detect_mode()
    config = get_github_config()

    print(f"Current mode: {mode}")
    print(f"GitHub: {config['owner']}/{config['repo']}")
    print("Permissions:")
    for key, value in config["permissions"].items():
        status = "✓" if value else "✗"
        print(f"  {status} {key}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
