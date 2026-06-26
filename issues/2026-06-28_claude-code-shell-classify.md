---
id: issue-2026-06-28-004
date: 2026-06-28
source: finding-4
relevance: 4/5
area: security-patterns
title: "Claude Code v2.1.193 — Shell Classification + Memory Pressure Reaping: Pattern Exploration"
status: open
tags:
  - claude-code
  - security
  - shell-classification
  - memory-management
  - patterns
---

# Claude Code v2.1.193 — Shell Classification + Memory Pressure Reaping

## Source
[Claude Code Release v2.1.193](https://github.com/anthropics/claude-code/releases/tag/v2.1.193)

## Summary
Claude Code v2.1.193 introduced several features with direct applicability to Hermes:

1. **`autoMode.classifyAllShell`**: Route ALL shell commands through classifier, not just arbitrary-code patterns — a universal approval guard
2. **Auto-mode denial reasons with transcript integration**: Classifier decisions are explainable and logged
3. **Automatic memory-pressure reaping**: Idle background shell commands are reaped under memory pressure
4. **Background task carryover fix**: Background tasks persist correctly across tool cycles

These patterns are directly applicable to Hermes' terminal tool and kernel manager.

## Impact Assessment
- **Shell classification**: Hermes currently classifies shell commands selectively — a universal classifier could improve security posture
- **Memory-pressure reaping**: Hermes' background process management could benefit from automatic cleanup
- **Risk**: Medium — architectural patterns, not drop-in code; requires design work

## Action Plan

1. [ ] **EXPLORE**: Study Claude Code's `classifyAllShell` pattern — how does universal classification work in practice?
2. [ ] **ASSESS**: Compare against Hermes' current shell approval guard in `hermes/tools/terminal.py`
3. [ ] **DESIGN**: Evaluate memory-pressure-aware background process reaping for Hermes' kernel manager
4. [ ] **PROTOTYPE**: If pattern fits, draft a design doc for Hermes universal shell classification
5. [ ] **CONSIDER**: Denial reason + transcript integration for audit trail

## Notes
- The `classifyAllShell` pattern shifts from "classify risky commands" to "classify everything" — simpler security model
- Memory-pressure reaping could prevent Hermes from accumulating zombie background processes
- These are architectural patterns to learn from, not code to port directly
