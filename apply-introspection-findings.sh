#!/bin/bash
# Implementation plan from 2026-06-28 FRP session introspection
# Created by Hydra cycle 2026-06-28 (00:31 UTC)
# If introspection subagent did NOT complete before session end, run this.

set -euo pipefail

# === 1. Create windows-remote skill ===
SKILLS_DIR="$HOME/.hermes/profiles/default/skills"
mkdir -p "$SKILLS_DIR"
cat > "$SKILLS_DIR/windows-remote.skill.md" << 'SKILLEOF'
# windows-remote

Interact with Windows machines. **CRITICAL: The `terminal` tool on this agent runs on Linux ONLY.** Do NOT run Windows-specific commands (PowerShell, schtasks, Enter-PSSession, Get-WmiObject, Get-* cmdlets) via the `terminal` tool — they will fail.

## Capabilities
- Instruct the user to run commands locally on their Windows machine.
- Use the `todo` tool to create a checklist the user follows on Windows.
- If PLEX-SERVER (the user's Windows machine) is reachable, guide the user through PowerShell remoting from their client — the agent cannot initiate it.
- Document exact commands for the user to copy-paste.

## Guardrails
- NEVER attempt PowerShell, schtasks, WinRM, Enter-PSSession, or any Get-* cmdlet in the `terminal` tool — it runs on Linux.
- NEVER pipe Windows commands through `ssh` expecting them to work on a remote Windows host — agent's terminal is Linux-only.
- When the user says "I'll do it on Windows," create a todo list with step-by-step commands. Do NOT try to execute them.
- For firmware flashing, ADB, or ODIN operations: instruct the user, do NOT attempt via terminal.
SKILLEOF

# === 2. Update android-adb skill with FRP patterns ===
SKILL_FILE="$SKILLS_DIR/android-adb.skill.md"
if [ -f "$SKILL_FILE" ]; then
    cat >> "$SKILL_FILE" << 'ADBEOF'

## FRP Bypass (Samsung SM-X205 / Unisoc T610)
- Identify tablet: `adb shell getprop ro.product.model`
- Firmware flashing: Use **Heimdall** (Linux) or **Odin** (Windows). User must use Windows for Odin.
- Test Point + EDL mode: Fallback for Unisoc T610. Requires opening device.
- SamFw FRP Tool: Software option (requires Windows + Samsung USB drivers).
- Recovery navigation: Use physical volume keys. "Apply update from ADB" in stock recovery may not work as expected.
ADBEOF
else
    cat > "$SKILL_FILE" << 'ADBEOF'
# android-adb

Android Debug Bridge utilities.

## FRP Bypass (Samsung SM-X205 / Unisoc T610)
- Identify tablet: `adb shell getprop ro.product.model`
- Firmware flashing: Use **Heimdall** (Linux) or **Odin** (Windows).
- Test Point + EDL mode: Fallback for Unisoc T610.
- SamFw FRP Tool: Windows-only software option.
- Recovery navigation: Volume keys; "Apply update from ADB" may not work.
ADBEOF
fi

# === 3. Create issue proposal for platform-capability awareness ===
mkdir -p "$HOME/.hermes/evolution/issues"
cat > "$HOME/.hermes/evolution/issues/2026-06-28_platform-capability-awareness.md" << 'ISSUEEOF'
# Platform Capability Awareness in System Prompt

The agent repeatedly attempted Windows administration (PowerShell, schtasks, Enter-PSSession) through the `terminal` tool, which runs on Linux. This wasted 8+ minutes and 40+ API calls.

**Proposal**: Add a "platform capability awareness" section to the system prompt that explicitly states:
- `terminal` runs on Linux
- Windows remote commands (PowerShell, schtasks, WinRM) will fail here
- When Windows administration is needed: create todo checklists for the user
- This prevents wasted compute and frustration

**Severity**: HIGH — prevents wasted time and API calls
**Effort**: LOW — one paragraph in system prompt + windows-remote skill
ISSUEEOF

echo "Done. Created: windows-remote skill, android-adb FRP update, platform-capability issue."
