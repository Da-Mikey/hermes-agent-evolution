---
name: windows-remote
description: >-
  Guardrails for interacting with remote Windows machines. Windows administration
  CANNOT be done through the `terminal` tool (which is Linux-only). The agent must
  instruct the user to run commands locally on Windows. Never attempt PowerShell,
  schtasks, or WinRM cmdlets in terminal.
category: devops
tags:
  - windows
  - remote
  - guardrail
  - platform-awareness
---

# Windows Remote — Agent Guardrails

**CRITICAL: The `terminal` tool runs on Linux. It is NOT capable of executing
PowerShell, schtasks, `Enter-PSSession`, or any Windows administration command.
Do NOT attempt to pipe Windows commands through `terminal`.**

## Core Principle

When a user asks you to perform an action on a Windows machine (local or remote):

1. **You CANNOT execute Windows commands yourself.** The `terminal` tool is a
   Linux shell. PowerShell, cmd.exe, `schtasks`, `Get-*` cmdlets, `Enter-PSSession`,
   and WinRM all require Windows — they will fail with "command not found" or
   produce garbage.

2. **You MUST instruct the user to run the command on their Windows machine.**
   Provide the exact command text, explain what it does, and ask them to paste
   the output back.

3. **You may use `web_search` and `read_file` to research Windows procedures.**
   That's your lane. Execution is the user's lane.

## What Goes Wrong

### Failure: Attempting PowerShell in `terminal`
```
terminal("Enter-PSSession -ComputerName PLEX-SERVER")
→ bash: Enter-PSSession: command not found
```
**Why it fails:** `Enter-PSSession` is a PowerShell cmdlet. It only exists on
Windows. The `terminal` tool runs bash.

### Failure: Attempting schtasks in `terminal`
```
terminal("schtasks /query /s PLEX-SERVER")
→ bash: schtasks: command not found
```
**Why it fails:** `schtasks` is a Windows executable. It does not exist on Linux.

### Failure: Attempting `Get-*` cmdlets
```
terminal("Get-Service -ComputerName PLEX-SERVER | Where-Object {$_.Status -eq 'Running'}")
→ bash: Get-Service: command not found
```
**Why it fails:** PowerShell pipeline syntax. Not valid bash.

## Correct Pattern: Instruct the User

Instead of:
```
❌ terminal("Enter-PSSession -ComputerName PLEX-SERVER")
```

Provide the user with:
```
✅ "Please run this on your Windows machine (PLEX-SERVER or any Windows PC
   on the same network), in PowerShell as Administrator:

   Enter-PSSession -ComputerName PLEX-SERVER

   Then run the following inside the session:

       schtasks /query /fo LIST /v | findstr /i "hermes"
       Get-Service | Where-Object {$_.Status -eq 'Running'} | Format-Table Name, DisplayName

   Paste the output back and I'll analyze it."
```

## When the User Has Linux-to-Windows Remotes

If the user has an SSH server on Windows (OpenSSH, not PowerShell Remoting),
you CAN use `terminal` with `ssh user@windows-host command`. But verify first:

1. Ask the user: "Do you have OpenSSH Server running on the Windows machine?"
2. If yes: commands like `ssh user@windows-host 'dir C:\'` may work through
   the Windows `cmd.exe` that OpenSSH launches.
3. This is still limited — PowerShell cmdlets won't work through a cmd.exe SSH
   session unless wrapped with `powershell -Command "..."`.

**Default stance:** assume you CANNOT execute Windows commands yourself. Ask
the user to run them locally.

## What You CAN Do

- **Research**: Use `web_search` to find Windows procedures, commands, and
  troubleshooting steps.
- **Write scripts**: Use `write_file` to create `.ps1` PowerShell scripts the
  user can download and run on their Windows machine.
- **Read Logs**: If Windows logs have been exported/shared as text files, you
  can `read_file` them.
- **Linux-to-Windows via SSH**: Only if user confirms OpenSSH is running on the
  Windows target, and you understand the cmd.exe limitations.

## Detection: Are You About to Make This Mistake?

**RED FLAGS — stop and use the "instruct user" pattern if you are about to:**

- Type `Enter-PSSession`, `Invoke-Command`, `New-PSSession`, or any `*-PSSession*`
  cmdlet into `terminal`
- Type `schtasks`, `sc.exe`, `net.exe`, `wmic`, `reg.exe`, or `msiexec` into
  `terminal`
- Type a command starting with `Get-`, `Set-`, `New-`, `Remove-`, `Enable-`,
  `Disable-`, `Start-`, `Stop-`, or `Restart-` that is a PowerShell cmdlet
- Type a pipe `|` followed by `Where-Object`, `Select-Object`, `Format-Table`,
  or `Out-File` (PowerShell-isms)
- Type `cmd /c` or `powershell -Command` into `terminal` — these will produce
  a bash "command not found" error because `cmd` and `powershell` binaries
  don't exist on Linux

**GREEN FLAGS — you ARE on the right track if you are:**

- Writing: "Please run this command on your Windows machine..."
- Using `web_search` to research Windows procedures
- Writing a `.ps1` file for the user to execute
- Using `ssh user@windows-host` after confirming OpenSSH is available

## Related Skills

- `windows-smb-remote`: SMB/CIFS file sharing from Linux to Windows (file access
  only, NOT administration). This skill covers mounting Windows shares — it does
  NOT enable running Windows commands.
- `android-adb`: Android device control via ADB (Linux host with USB or wireless
  debugging connection to Android target).

## Memory Hook

**WINDOWS COMMANDS → USER'S KEYBOARD.** Never yours via `terminal`.
