---
id: issue-2026-06-28-005
date: 2026-06-28
source: introspection-2026-06-28 recommendation
relevance: 4/5
area: agent-core
title: "Add platform-capability-awareness to the system prompt"
status: open
tags:
  - system-prompt
  - guardrail
  - platform-awareness
  - windows
  - terminal
---

# Add platform-capability-awareness to the system prompt

## Source
[Introspection Report 2026-06-28](../introspection/2026-06-28-introspection.md),
Section "Actionable Recommendation for Prometheus"

## Problem
During the 2026-06-27 FRP bypass session, the agent repeatedly attempted
Windows administration commands through the `terminal` tool:
- `Enter-PSSession`, `schtasks`, PowerShell `Get-*` cmdlets
- 13 of 30 terminal calls produced WARNINGs
- Agent burned 8+ minutes and 40+ API calls retrying failing commands
- The root cause: the agent lacks awareness that `terminal` is a Linux shell
  and cannot execute Windows binaries or PowerShell cmdlets

The `windows-remote` skill (created as a separate implementation) addresses the
specific Windows remote case, but the deeper pattern is broader: **the agent
should know what its tools can and cannot do at a platform level.**

## Proposed Change

Add a "Platform Capabilities" section to the Hermes system prompt. This section
should explicitly state:

1. **The `terminal` tool runs on Linux (bash).** It cannot execute:
   - PowerShell cmdlets (`Get-*`, `Set-*`, `Enter-PSSession`, etc.)
   - Windows executables (`schtasks.exe`, `sc.exe`, `net.exe`, `reg.exe`, `wmic.exe`)
   - macOS-specific commands (`osascript`, `defaults`, `pbcopy`/`pbpaste`)
   - Android-specific binaries (these require `adb shell`)

2. **For non-Linux targets, the agent's role is to INSTRUCT the user, not execute.**
   Provide exact commands for the user to run on their platform and paste results back.

3. **ADB and SSH are the bridges.** The `terminal` tool CAN reach:
   - Android devices via `adb shell` (USB or wireless)
   - Remote Linux hosts via `ssh`
   - Windows hosts via `ssh` IF OpenSSH is confirmed running (cmd.exe context, not PowerShell)

4. **The agent runs on Linux — state it explicitly.** Source: `Host: Linux (6.17.0-35-generic)`.
   This is already in the system prompt preamble but is easy to overlook.

## Where to Add

The system prompt is defined in Hermes' agent configuration. The exact file
depends on the deployment:

- **Hermes Agent (fork):** `~/.hermes/hermes-agent/hermes_cli/prompts.py` or similar
- **Upstream Hermes:** System prompt template in the agent configuration module

Suggested insertion point: After the "Host: Linux ..." line in the system prompt
preamble, or as a new "## Platform Capabilities" section.

## Draft Text

```markdown
## Platform Capabilities

You are running on **Linux (bash shell via `terminal` tool)**. This has implications:

**You CANNOT execute:**
- Windows binaries or PowerShell cmdlets (these must run on the user's Windows machine)
- macOS-specific commands (these must run on the user's Mac)
- Android commands directly (use `adb shell` to reach Android devices)

**You CAN reach other platforms through bridges:**
- **Android:** `adb shell` (USB or wireless debugging)
- **Remote Linux:** `ssh user@host`
- **Windows (limited):** `ssh user@windows-host` if OpenSSH is running (cmd.exe only, not PowerShell)

**When the user needs Windows/macOS commands run:**
- Provide the exact command text
- Ask the user to run it on their machine
- Ask them to paste the output back
- NEVER attempt to run platform-specific commands in `terminal`
```

## Impact Assessment

- **Severity:** Medium — wasted 8+ minutes in one session; would recur
- **Risk:** Low — adding declarative text to system prompt, no code changes
- **Scope:** Small — ~15 lines added to system prompt template
- **Dependencies:** None

## Related

- `windows-remote` skill (implementation created from same introspection)
- `android-adb` skill (already covers ADB bridging patterns)
- Hermes Agent system prompt: deployed in `~/.hermes/hermes-agent/`

## Action Plan

1. [ ] **LOCATE**: Find the system prompt definition in `~/.hermes/hermes-agent/hermes_cli/`
2. [ ] **DRAFT**: Write the exact text to insert (15 lines, see draft above)
3. [ ] **INSERT**: Add after the Host platform declaration line
4. [ ] **TEST**: Verify the agent no longer attempts `Enter-PSSession` in `terminal` in staged tests
5. [ ] **DOCUMENT**: Update `SKILL.md` for `hermes-agent` skill to mention platform awareness

### Git commands that would be needed (NOT to be executed):

```bash
# cd ~/.hermes/hermes-agent
# git checkout -b feat/platform-capability-awareness-prompt
# # Edit the system prompt file
# git add hermes_cli/prompts.py  # or wherever the system prompt lives
# git commit -m "feat: add platform-capability-awareness to system prompt

# Explicitly states terminal is Linux-only, PowerShell/Windows commands
# must run on user's machine. Prevents agent from wasting turns attempting
# Windows administration via terminal tool."
# gh pr create --title "Add platform-capability-awareness to system prompt" \
#   --body "Prevents agent from attempting Windows/macOS commands in terminal. \
#     From introspection 2026-06-28 finding."
```
