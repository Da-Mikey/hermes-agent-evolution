#!/bin/bash
# ============================================================================
# Implementation plan from 2026-06-28 introspection run #2
# Created by Hydra cycle 2026-06-28 (introspection on 2 cron session dumps)
#
# Findings to implement:
#   1. Adaptive cron frequency — reduce Hydra runs during blocked pipeline states
#   2. Tighten cron toolset — remove 22 MCP tqmemory tools from Hydra cron sessions
#   3. Overnight circuit breaker — skip cycles after consecutive API failures
#
# These are PROMPT-LEVEL and CONFIG-LEVEL changes. No code changes needed.
# ============================================================================
set -euo pipefail

EVOLUTION_DIR="$HOME/.hermes/evolution"
CRON_DIR="$HOME/.hermes/cron"

echo "=== Improvement 1: Adaptive Hydra Prompt (HIGH impact) ==="
echo ""
echo "Current: Hydra runs every 30 minutes, calls DeepSeek API, processes"
echo "30-tool schema, reads state.json, and 95% of the time returns [SILENT]"
echo "because the pipeline is structurally blocked."
echo ""
echo "Proposed change: Add a pre-flight check at the TOP of the Hydra prompt"
echo "that reads state.json first and short-circuits when appropriate."
echo ""
echo "New preamble for Hydra prompt (to be added BEFORE the existing prompt):"
echo ""
cat << 'PROMPTEOF'
# Pipeline State Check (RUN THIS FIRST)

Before activating any heads, read ~/.hermes/evolution/state.json and check:

1. Is `blockers.pipeline_blocked` == true?
2. Is `state.json` less than 2 hours old (modified time)?
3. Are `feedback_requests.pending` unchanged from previous cycle?
4. Has the upstream commit count changed (< 10 new commits)?

If ALL of the above are true: respond immediately with [SILENT].
Do NOT read any other files. Do NOT activate any heads.
Do NOT spawn subagents. Just [SILENT].

This check saves ~15,000 tokens per run × 48 runs/day = 720K tokens/day
during blocked pipeline states.

If any condition is FALSE (pipeline unblocked, state file >2h old, feedback
added, upstream advanced significantly), proceed with normal Hydra workflow.
PROMPTEOF

echo ""
echo "Deployment: Edit the cron job prompt in ~/.hermes/cron/jobs.json"
echo "for job ID 0b013aa0bd43. Add the above preamble at the start."
echo ""

echo "=== Improvement 2: Tighten Hydra Toolset (MEDIUM impact) ==="
echo ""
echo "Current: Hydra cron sessions include 30 tools in the API request:"
echo "  - delegate_task (1)"
echo "  - Terminal + file tools (8: terminal, read_file, write_file, patch,"
echo "    search_files, repo_map, process)"
echo "  - MCP tqmemory tools (22: mcp_tqmemory_*)"
echo ""
echo "The 22 MCP tqmemory tools are NEVER used by Hydra. They consume ~8KB"
echo "of context window and increase API latency."
echo ""
echo "Proposed: The jobs.json already sets enabled_toolsets to"
echo '  ["file", "delegation", "terminal"]'
echo "but the MCP tools are still being included in the API request."
echo ""
echo "Fix: Ensure the cron runner respects enabled_toolsets by filtering"
echo "out MCP tools when building the API request for cron sessions."
echo ""
echo "Target file: ~/.hermes/hermes-agent/hermes_cli/cron_runner.py"
echo "(or wherever cron API requests are constructed)"
echo ""
echo "Pseudocode:"
cat << 'CODEEOF'
def build_cron_tools(job_config, all_tools):
    enabled = job_config.get("enabled_toolsets", [])
    if "terminal" in enabled:
        # Include terminal, read_file, write_file, search_files, patch, repo_map
        pass
    if "delegation" in enabled:
        # Include delegate_task
        pass
    if "file" in enabled:
        # Include read_file, write_file, patch, search_files
        pass
    # Always EXCLUDE mcp_tqmemory_* from cron unless explicitly listed
    mcp_tools = [t for t in all_tools if t["function"]["name"].startswith("mcp_")]
    # Only include MCP tools if enabled_toolsets contains "memory"
    if "memory" not in enabled:
        all_tools = [t for t in all_tools if t not in mcp_tools]
    return all_tools
CODEEOF

echo ""
echo "=== Improvement 3: Overnight Circuit Breaker (LOW priority) ==="
echo ""
echo "Pattern: 10 genuine failures in 374 total runs (2.7%). 6 broken pipe,"
echo "4 connection errors. All in the 23:00-06:00 UTC window."
echo ""
echo "Proposed: Add a failure counter check at the cron runner level."
echo "If a job fails 2+ times in a row with timeout/connection errors,"
echo "skip the next 2 cycles (silent skip, no API call)."
echo ""
echo "Pseudocode (in cron runner):"
cat << 'CBEOF'
consecutive_timeout_failures = 0
MIN_SKIP_CYCLES = 2

def should_run_job(job):
    global consecutive_timeout_failures
    if consecutive_timeout_failures >= THRESHOLD:
        if skipped_cycles < MIN_SKIP_CYCLES:
            skipped_cycles += 1
            return False  # Silent skip
        else:
            consecutive_timeout_failures = 0
            skipped_cycles = 0
            return True  # Retry after cooling off

def on_job_failure(job, error):
    if error.failure_category == "timeout":
        consecutive_timeout_failures += 1
    else:
        consecutive_timeout_failures = 0
CBEOF

echo ""
echo "=== Summary ==="
echo "3 improvements documented (prompt-level + config-level):"
echo "  1. Adaptive cron frequency — prompt preamble (HIGH impact, ~720K tokens/day saved)"
echo "  2. Cron toolset tightening — MCP tool filter (~8KB/request saved)"
echo "  3. Overnight circuit breaker — cron runner logic (reduces noise)"
echo ""
echo "No code changes implemented yet — awaiting human feedback on the"
echo "2 pending feedback requests before any implementation proceeds."
echo "(Pipeline policy: implementation blocked while feedback is pending)"
