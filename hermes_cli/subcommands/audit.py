"""``hermes audit`` subcommand parser (issue #3065).

Structured audit trail inspection, verification, and causal DAG reconstruction
for autonomous agent sessions and subagent work.
"""

from __future__ import annotations

from typing import Callable


def build_audit_parser(subparsers, *, cmd_audit: Callable) -> None:
    """Attach the ``audit`` subcommand to ``subparsers``."""
    audit_parser = subparsers.add_parser(
        "audit",
        help="Inspect and verify the tamper-evident structured audit trail",
        description=(
            "Query, verify, and reconstruct the action -> artifact -> validation "
            "trail for autonomous runs and subagent delegations."
        ),
    )
    audit_subparsers = audit_parser.add_subparsers(
        dest="audit_command",
        metavar="<subcommand>",
    )

    # 1. verify
    verify_parser = audit_subparsers.add_parser(
        "verify",
        help="Verify the cryptographic integrity of the audit trail hash chain",
    )
    verify_parser.add_argument(
        "--path",
        default=None,
        help="Custom path to audit trail JSONL file",
    )

    # 2. show
    show_parser = audit_subparsers.add_parser(
        "show",
        help="Reconstruct action -> artifact -> validation DAG for a session",
    )
    show_parser.add_argument(
        "session_id",
        help="Session ID to inspect",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted text",
    )
    show_parser.add_argument(
        "--path",
        default=None,
        help="Custom path to audit trail JSONL file",
    )

    # 3. list / query
    list_parser = audit_subparsers.add_parser(
        "list",
        help="List recent audit trail events",
    )
    list_parser.add_argument(
        "--session-id",
        default=None,
        help="Filter by session ID",
    )
    list_parser.add_argument(
        "--event-type",
        default=None,
        choices=["action", "artifact", "validation", "delegation"],
        help="Filter by event type",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum events to return (default: 50)",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON",
    )

    # 4. prune
    prune_parser = audit_subparsers.add_parser(
        "prune",
        help="Prune entries older than retention window and re-anchor hash chain",
    )
    prune_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Override retention window in days",
    )

    audit_parser.set_defaults(func=cmd_audit)
