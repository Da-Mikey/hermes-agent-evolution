#!/bin/bash
# auto_update.sh — daily self-update of Hermes Evolution from its GitHub fork.
#
# RUN FROM SYSTEM CRON / SYSTEMD TIMER — NOT as a Hermes cron job.
# Rationale: the Hermes cron ticker runs as a background THREAD inside the
# gateway process (gateway/run.py::_start_cron_ticker). A self-update job that
# restarts the gateway from within that thread would kill itself mid-update,
# possibly during `git pull` / `setup` -> corrupted install. An independent
# process can safely update code and restart the gateway.
#
# Safety model:
#   * No-op fast when there are no upstream changes (cheap git fetch + compare).
#   * Single instance via flock.
#   * Fast-forward-only pull (never merges / rewrites local work).
#   * Backs up the data dir BEFORE applying, keeps the last N backups.
#   * Health-checks the new code; ROLLS BACK code + data on ANY failure.
#
# Usage:  scripts/auto_update.sh [--force]
# Env:    HERMES_EVOLUTION_DIR  repo path   (default ~/hermes-agent-evolution)
#         HERMES_HOME           data dir    (default ~/.hermes)
#         AUTO_UPDATE_BRANCH    branch      (default main)

set -uo pipefail   # intentionally NOT -e: failures are handled for rollback

# Make tools resolvable under a minimal system-cron PATH.
REPO_DIR="${HERMES_EVOLUTION_DIR:-$HOME/hermes-agent-evolution}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$REPO_DIR/venv/bin:$PATH"

HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
BRANCH="${AUTO_UPDATE_BRANCH:-main}"
LOG_DIR="$HOME_DIR/logs"
LOG="$LOG_DIR/auto-update.log"
LOCK="$HOME_DIR/.auto-update.lock"
KEEP_BACKUPS=3

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

mkdir -p "$LOG_DIR"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >>"$LOG"; }

# --- single instance -------------------------------------------------------
exec 9>"$LOCK" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
    flock -n 9 || { log "Another auto-update is running; skip."; exit 0; }
fi

log "=== auto-update start (repo=$REPO_DIR home=$HOME_DIR branch=$BRANCH) ==="

cd "$REPO_DIR" 2>/dev/null || { log "ERROR: repo dir not found: $REPO_DIR"; exit 1; }
if [ ! -d .git ]; then log "ERROR: $REPO_DIR is not a git repo"; exit 1; fi

# --- 1) detect changes -----------------------------------------------------
if ! git fetch --quiet origin "$BRANCH" 2>>"$LOG"; then
    log "ERROR: git fetch failed (network/auth?). Abort."
    exit 1
fi
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "")
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")
if [ -z "$LOCAL" ] || [ -z "$REMOTE" ]; then
    log "ERROR: cannot resolve HEAD or origin/$BRANCH. Abort."
    exit 1
fi
if [ "$LOCAL" = "$REMOTE" ] && [ "$FORCE" -ne 1 ]; then
    log "Up to date ($LOCAL). No update needed."
    exit 0
fi
log "Update available: $LOCAL -> $REMOTE"

# --- 2) update code (fast-forward only) ------------------------------------
# Done before backup so a failed/diverged pull leaves data untouched.
if ! git pull --ff-only origin "$BRANCH" >>"$LOG" 2>&1; then
    log "ERROR: 'git pull --ff-only' failed (local commits/divergence?). Abort, no changes applied."
    exit 1
fi

# --- 3) backup data dir (+ prune old) --------------------------------------
BACKUP="$HOME_DIR.autoupdate.backup.$(date +%Y%m%d_%H%M%S)"
if [ -d "$HOME_DIR" ]; then
    if cp -r "$HOME_DIR" "$BACKUP" 2>>"$LOG"; then
        log "Backup: $BACKUP"
        ls -dt "$HOME_DIR".autoupdate.backup.* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) \
            | while read -r old; do rm -rf "$old" && log "Pruned old backup: $old"; done
    else
        log "ERROR: backup failed — rolling back code and aborting."
        git reset --hard "$LOCAL" >>"$LOG" 2>&1
        exit 1
    fi
fi

rollback() {
    log "ROLLBACK -> code $LOCAL + data from backup"
    git reset --hard "$LOCAL" >>"$LOG" 2>&1 || log "WARN: git reset failed"
    bash setup-hermes.sh >>"$LOG" 2>&1 || log "WARN: rollback setup failed"
    if [ -d "$BACKUP" ]; then
        rm -rf "$HOME_DIR" && mv "$BACKUP" "$HOME_DIR" && log "Restored data from backup"
    fi
    hermes gateway restart >>"$LOG" 2>&1 || true
    log "=== auto-update ROLLED BACK to $LOCAL; MANUAL REVIEW NEEDED ==="
}

# --- 4) re-run setup (deps + seed skills) ----------------------------------
if ! bash setup-hermes.sh >>"$LOG" 2>&1; then
    log "ERROR: setup-hermes.sh failed on new code."
    rollback
    exit 1
fi

# --- 5) register evolution cron jobs (idempotent) --------------------------
if [ -x venv/bin/python ] && [ -f scripts/register_evolution_cron.py ]; then
    venv/bin/python scripts/register_evolution_cron.py >>"$LOG" 2>&1 \
        || log "WARN: evolution cron registration reported issues"
fi

# --- 6) health checks — does the new code actually run? ---------------------
if ! hermes --version >>"$LOG" 2>&1; then
    log "ERROR: health check 'hermes --version' failed on new code."
    rollback
    exit 1
fi
if ! hermes cron list >>"$LOG" 2>&1; then
    log "ERROR: health check 'hermes cron list' failed on new code."
    rollback
    exit 1
fi
log "Health checks passed."

# --- 7) restart gateway so the running process picks up new code -----------
# `hermes gateway restart` is restart-aware (graceful drain from a shell).
if hermes gateway status >/dev/null 2>&1; then
    if hermes gateway restart >>"$LOG" 2>&1; then
        log "Gateway restarted on new code."
    else
        log "WARN: gateway restart failed; new code active on next start."
    fi
else
    log "Gateway not running; new code active on next start."
fi

log "=== auto-update SUCCESS: now at $REMOTE ==="
exit 0
