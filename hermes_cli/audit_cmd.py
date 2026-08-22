"""Implementation of ``hermes audit`` CLI commands (issue #3065)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from agent import audit_trail


def cmd_audit(args) -> None:
    subcmd = getattr(args, "audit_command", None) or "verify"

    if subcmd == "verify":
        path = Path(args.path) if getattr(args, "path", None) else None
        valid, count = audit_trail.verify(path)
        if valid:
            print(f"✓ Audit trail valid: hash chain intact ({count} events verified).")
        else:
            print(f"✗ Audit trail integrity verification FAILED at entry {count}!", file=sys.stderr)
            sys.exit(1)

    elif subcmd == "show":
        session_id = args.session_id
        path = Path(args.path) if getattr(args, "path", None) else None
        recon = audit_trail.reconstruct_run(session_id, path=path)

        if getattr(args, "json", False):
            print(json.dumps(recon, indent=2, ensure_ascii=False))
            return

        print(f"═══ Audit Trail for Session: {session_id} ═══")
        chain_icon = "✓" if recon["valid_chain"] else "✗"
        print(f"Global Hash-Chain Integrity: {chain_icon} ({recon['event_count']} events in session)")
        summary = recon["summary"]
        print(
            f"Summary: {summary['actions_count']} actions, "
            f"{summary['artifacts_count']} artifacts, "
            f"{summary['validations_count']} validations, "
            f"{summary['delegations_count']} delegations "
            f"(success rate: {summary['success_rate'] * 100:.1f}%)"
        )
        print()

        if recon["artifacts"]:
            print("📦 Artifacts:")
            for art in recon["artifacts"]:
                print(f"  • {art}")
            print()

        if recon["validations"]:
            print("🧪 Validations:")
            for val in recon["validations"]:
                print(f"  • {val}")
            print()

        if recon["delegations"]:
            print("🔀 Subagent Delegations:")
            for d in recon["delegations"]:
                status = d.get("status", "unknown")
                tid = d.get("task_id", "subagent")
                meta = d.get("metadata", {})
                goal = meta.get("goal") or d.get("inputs_digest") or ""
                print(f"  [{status}] {tid}: {str(goal)[:80]}")
            print()

        if recon["actions"]:
            print("⚡ Actions:")
            for act in recon["actions"][:30]:
                tool = act.get("tool_name", "unknown")
                st = act.get("status", "ok")
                ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(act.get("ts", 0)))
                print(f"  {ts_str} [{st}] {tool}")
            if len(recon["actions"]) > 30:
                print(f"  ... and {len(recon['actions']) - 30} more actions")

    elif subcmd == "list":
        session_id = getattr(args, "session_id", None)
        event_type = getattr(args, "event_type", None)
        limit = getattr(args, "limit", 50)
        path = Path(args.path) if getattr(args, "path", None) else None
        events = audit_trail.query_trail(
            session_id=session_id, event_type=event_type, limit=limit, path=path
        )

        if getattr(args, "json", False):
            print(json.dumps([e["payload"] for e in events], indent=2, ensure_ascii=False))
            return

        print(f"Found {len(events)} audit event(s):")
        for e in events:
            p = e["payload"]
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.get("ts", 0)))
            etype = p.get("event_type", "action")
            sid = p.get("session_id", "")[:12]
            tool = p.get("tool_name") or p.get("status", "")
            print(f"{ts_str} | {etype:<11} | {sid} | {tool}")

    elif subcmd == "prune":
        days = getattr(args, "days", None)
        path = Path(args.path) if getattr(args, "path", None) else None
        now = time.time()
        removed = audit_trail.prune(days=days, now=now, path=path)
        print(f"✓ Pruned {removed} expired audit trail event(s). Hash chain re-anchored.")

    else:
        print(f"unknown audit subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)

