#!/bin/bash
# install_auto_update.sh — schedule the daily Hermes Evolution self-update.
#
# Adds a SYSTEM cron entry that runs scripts/auto_update.sh once a day. The
# self-update MUST run from system cron (not the Hermes in-gateway cron ticker)
# so it can restart the gateway without killing itself — see auto_update.sh.
#
# Idempotent: re-running replaces the existing entry (matched by a marker).
#
# Usage:   scripts/install_auto_update.sh            # install (default ~04:17)
#          AUTO_UPDATE_SCHEDULE="30 5 * * *" scripts/install_auto_update.sh
#          scripts/install_auto_update.sh --remove   # uninstall
# Env:     HERMES_EVOLUTION_DIR  repo path (default ~/hermes-agent-evolution)

set -euo pipefail

MARKER="hermes-evolution-auto-update"
REPO_DIR="${HERMES_EVOLUTION_DIR:-$HOME/hermes-agent-evolution}"
SCRIPT="$REPO_DIR/scripts/auto_update.sh"
# Off-zero minute on purpose (avoid the :00 thundering herd).
SCHEDULE="${AUTO_UPDATE_SCHEDULE:-17 4 * * *}"
LOG="${HERMES_HOME:-$HOME/.hermes}/logs/auto-update.log"

if ! command -v crontab >/dev/null 2>&1; then
    echo "❌ 'crontab' not found. Use a systemd timer instead (see AUTO_UPGRADE.md)." >&2
    exit 1
fi

# --remove: strip the marker line and exit
if [ "${1:-}" = "--remove" ]; then
    REMAIN="$(crontab -l 2>/dev/null | grep -v "$MARKER" || true)"
    if [ -n "$REMAIN" ]; then
        printf '%s\n' "$REMAIN" | crontab -
    else
        crontab -r 2>/dev/null || true   # nothing left — clear the crontab
    fi
    echo "✅ Removed Hermes Evolution auto-update cron entry."
    exit 0
fi

if [ ! -f "$SCRIPT" ]; then
    echo "❌ auto_update.sh not found at: $SCRIPT" >&2
    exit 1
fi
chmod +x "$SCRIPT"

# HERMES_HOME is exported into the entry only if explicitly set, so the job
# targets the same data dir as this install.
ENV_PREFIX=""
if [ -n "${HERMES_HOME:-}" ]; then ENV_PREFIX="HERMES_HOME=$HERMES_HOME "; fi
if [ -n "${HERMES_EVOLUTION_DIR:-}" ]; then ENV_PREFIX="${ENV_PREFIX}HERMES_EVOLUTION_DIR=$HERMES_EVOLUTION_DIR "; fi

ENTRY="$SCHEDULE ${ENV_PREFIX}$SCRIPT >> $LOG 2>&1  # $MARKER"

# Idempotent install: drop any existing marker line, then append the fresh one.
# Built explicitly (not via a subshell pipe) to avoid set -e/pipefail wiping or
# aborting when no crontab exists yet — an empty `crontab -l` must be harmless.
KEPT="$(crontab -l 2>/dev/null | grep -v "$MARKER" || true)"
if [ -n "$KEPT" ]; then
    printf '%s\n%s\n' "$KEPT" "$ENTRY" | crontab -
else
    printf '%s\n' "$ENTRY" | crontab -
fi

echo "✅ Installed daily self-update cron entry:"
crontab -l | grep "$MARKER" | sed 's/^/   /'
echo ""
echo "📂 Log:        $LOG"
echo "🧪 Test now:   $SCRIPT --force"
echo "🗑  Uninstall:  $0 --remove"
