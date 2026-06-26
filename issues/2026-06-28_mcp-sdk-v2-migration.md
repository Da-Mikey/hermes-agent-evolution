---
id: issue-2026-06-28-001
date: 2026-06-28
source: finding-1
relevance: 5/5
area: mcp-integration
title: "MCP Python SDK v2.0.0a3 — Major Protocol Rewrite: Prepare Migration"
status: open
tags:
  - mcp
  - breaking-change
  - protocol
  - upstream
---

# MCP Python SDK v2.0.0a3 — Major Protocol Rewrite: Prepare Migration

## Source
[GitHub Release: MCP Python SDK v2.0.0a3](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0a3)

## Summary
The Model Context Protocol Python SDK released v2.0.0a3 (2026-06-26) with a complete protocol overhaul targeting the 2026-07-28 spec. Key changes:

- The `initialize` handshake is replaced by a stateless per-POST self-describing pattern.
- `ClientSession` gains `.discover()`/`.adopt()` alongside `.initialize()`.
- Protocol types split into a standalone `mcp-types` package.

This is a **BREAKING change** — Hermes' MCP integration (`hermes/mcp/`) will need rewriting when it upgrades beyond 1.x.

## Impact Assessment
- **Hermes MCP integration**: Full rewrite required for post-1.x compatibility
- **Timeline**: Alpha — API is in flux, do NOT upgrade yet
- **Risk**: High — protocol-level breakage, not a drop-in replacement

## Action Plan

1. [ ] **READ**: Study the [migration guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
2. [ ] **TRACK**: Monitor `LATEST_PROTOCOL_VERSION` → `"2026-07-28"` for stabilization
3. [ ] **ASSESS**: Identify all `hermes/mcp/` components that touch the protocol layer (handshake, session init, type imports)
4. [ ] **DESIGN**: Draft a migration plan for Hermes once SDK stabilizes (beta/rc)
5. [ ] **WAIT**: Do NOT upgrade until a stable release exists

## Notes
- Current Hermes MCP integration targets SDK 1.x — fully functional now
- The stateless `discover`/`adopt` pattern may enable cleaner Hermes MCP server lifecycle management
- `mcp-types` split may allow Hermes to share types without the full SDK dependency
