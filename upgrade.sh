#!/bin/bash
# upgrade.sh v2.3 - Automatic Upgrade Script for Hermes Evolution
# Migrates an existing Hermes Agent install onto the Hermes Evolution fork
# WITHOUT data loss. Works on any system with `hermes` already installed.
# Source: https://github.com/Lexus2016/hermes-agent-evolution
#
# v2.x root-cause design:
#   * Evolution skills are BUNDLED under the canonical
#     skills/<category>/<skill>/SKILL.md layout. setup-hermes.sh seeds them
#     into the REAL skills dir via tools/skills_sync.py — no manual copying,
#     no hard-coded ~/.hermes path.
#   * Evolution cron jobs are REGISTERED into Hermes' native jobs.json registry
#     via scripts/register_evolution_cron.py (copying YAML does nothing).
#   * HERMES_HOME is resolved by ASKING Hermes, not assumed.
#   * The running gateway is restarted so it reloads new code + skills
#     (opt out with --no-restart or HERMES_SKIP_GATEWAY_RESTART=1).
#
# Usage: bash upgrade.sh [--no-restart] [--no-auto-update]

set -euo pipefail

# --- options ---------------------------------------------------------------
SKIP_RESTART="${HERMES_SKIP_GATEWAY_RESTART:-0}"
# Self-evolution is the POINT of this fork: daily auto-update is ON by default.
# Opt out with --no-auto-update or HERMES_NO_AUTO_UPDATE=1.
AUTO_UPDATE=1
if [ "${HERMES_NO_AUTO_UPDATE:-0}" = "1" ]; then AUTO_UPDATE=0; fi
for arg in "$@"; do
    case "$arg" in
        --no-restart) SKIP_RESTART=1 ;;
        --no-auto-update) AUTO_UPDATE=0 ;;
        --with-auto-update) AUTO_UPDATE=1 ;;  # back-compat; now the default
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "🧬 Hermes Evolution Automatic Upgrade v2.1"
echo "=========================================="
echo ""

# Configuration
EVOLUTION_REPO="https://github.com/Lexus2016/hermes-agent-evolution.git"
EVOLUTION_DIR="$HOME/hermes-agent-evolution"
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)

# Detect that Hermes is installed
echo "🔍 Detecting Hermes installation..."
if ! command -v hermes >/dev/null 2>&1; then
    echo "❌ 'hermes' command not found. Install Hermes Agent first."
    exit 1
fi
HERMES_PROJECT=$(hermes --version 2>/dev/null | grep "Project:" | cut -d' ' -f2 || echo "")
echo "✅ Hermes detected${HERMES_PROJECT:+ at: $HERMES_PROJECT}"
echo ""

# Step 1: Backup the live Hermes data dir
echo "📦 Step 1/7: Creating backup..."
PRELIM_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [ -d "$PRELIM_HOME" ]; then
    cp -r "$PRELIM_HOME" "$PRELIM_HOME.backup.$BACKUP_DATE"
    echo "✅ Backup created: $PRELIM_HOME.backup.$BACKUP_DATE"
else
    echo "ℹ️  No existing Hermes data dir at $PRELIM_HOME (fresh install)"
fi

# Step 2: Clean previous attempts and clone the fork
echo ""
echo "📥 Step 2/7: Cloning Hermes Evolution..."
rm -rf "$EVOLUTION_DIR" /tmp/hermes-evolution
git clone --depth 1 "$EVOLUTION_REPO" "$EVOLUTION_DIR"
echo "✅ Cloned to: $EVOLUTION_DIR"

# Step 3: Run setup. Installs new code AND seeds bundled skills (incl. evolution/*)
echo ""
echo "🔧 Step 3/7: Running setup-hermes.sh (installs code + seeds skills)..."
cd "$EVOLUTION_DIR"
if [ ! -f "setup-hermes.sh" ]; then
    echo "❌ setup-hermes.sh not found in repo!"
    exit 1
fi
bash setup-hermes.sh
echo "✅ Setup completed"

# Resolve the REAL Hermes home by asking Hermes itself.
HERMES_HOME_RESOLVED=$("$EVOLUTION_DIR/venv/bin/python" -c \
    "import sys; sys.path.insert(0, '$EVOLUTION_DIR'); from hermes_constants import get_hermes_home; print(get_hermes_home())" \
    2>/dev/null || echo "${HERMES_HOME:-$HOME/.hermes}")
echo "📂 Hermes home resolved to: $HERMES_HOME_RESOLVED"

# Step 4: Verify evolution skills were seeded into the dir Hermes scans
echo ""
echo "📚 Step 4/7: Verifying evolution skills were seeded..."
SEEDED=$(find "$HERMES_HOME_RESOLVED/skills" -path "*/evolution/*/SKILL.md" 2>/dev/null | wc -l | tr -d ' ')
if [ "$SEEDED" -gt 0 ]; then
    echo "✅ $SEEDED evolution skill(s) present under $HERMES_HOME_RESOLVED/skills/evolution/"
else
    echo "⚠️  No evolution skills found — check tools/skills_sync.py output above"
fi

# Step 5: Register evolution cron jobs into Hermes' native jobs.json registry.
# Copying YAML files does NOT schedule anything — the scheduler only reads
# jobs.json. This converter is idempotent (by job name): safe to re-run.
echo ""
echo "⏰ Step 5/7: Registering evolution cron jobs..."
if [ -f "$EVOLUTION_DIR/scripts/register_evolution_cron.py" ]; then
    "$EVOLUTION_DIR/venv/bin/python" "$EVOLUTION_DIR/scripts/register_evolution_cron.py" \
        || echo "⚠️  Cron registration reported issues (see output above)"
else
    echo "⚠️  register_evolution_cron.py not found — skipping cron registration"
fi

# Step 6: Restart the gateway so the running process reloads new code + skills.
# `hermes gateway restart` is restart-aware: from a shell it does a graceful
# drain-restart; from within the gateway (self-update) it requests an async
# SIGUSR1 self-restart. Opt out with --no-restart / HERMES_SKIP_GATEWAY_RESTART.
echo ""
echo "🔄 Step 6/7: Restarting gateway..."
if [ "$SKIP_RESTART" = "1" ]; then
    echo "⏭️  Skipped (--no-restart). Apply later with: hermes gateway restart"
elif hermes gateway status >/dev/null 2>&1; then
    if hermes gateway restart >/dev/null 2>&1; then
        echo "✅ Gateway restarted — now running new code + skills"
    else
        echo "⚠️  Gateway restart failed. Restart manually: hermes gateway restart"
    fi
else
    echo "ℹ️  Gateway not running — it will pick up changes on next start"
fi

# Step 7: Final verification
echo ""
echo "✅ Step 7/7: Verifying installation..."
if hermes skills list 2>/dev/null | grep -qi "evolution"; then
    echo "✅ Evolution skills are visible to Hermes:"
    hermes skills list 2>/dev/null | grep -i "evolution" | sed 's/^/   /'
else
    echo "⚠️  Evolution skills not visible — inspect $HERMES_HOME_RESOLVED/skills/evolution/"
fi
echo ""
echo "📋 Evolution cron jobs:"
hermes cron list 2>/dev/null | grep -i "evolution" | sed 's/^/   /' || echo "   (run: hermes cron list)"

# Self-evolution: schedule the daily GitHub self-update via SYSTEM cron.
# ON by default (this is the whole point of the fork). Disable per the message.
if [ "$AUTO_UPDATE" = "1" ]; then
    echo ""
    echo "🔁 Installing daily self-update (system cron) — this is what makes the agent self-evolving..."
    HERMES_EVOLUTION_DIR="$EVOLUTION_DIR" bash "$EVOLUTION_DIR/scripts/install_auto_update.sh" \
        || echo "⚠️  Could not install auto-update cron (see output above)"
else
    echo ""
    echo "⏭️  Auto-update DISABLED (--no-auto-update / HERMES_NO_AUTO_UPDATE=1)."
    echo "    The agent will NOT self-evolve. Enable later with:"
    echo "    bash $EVOLUTION_DIR/scripts/install_auto_update.sh"
fi

echo ""
echo "=========================================="
echo "🎉 Upgrade to Hermes Evolution complete!"
echo ""
echo "📖 What's new:"
echo "  • Evolution skills bundled (research, issues, analysis, implementation, upstream-sync)"
echo "  • Evolution cron jobs registered in the native scheduler"
if [ "$AUTO_UPDATE" = "1" ]; then
    echo "  • Daily GitHub self-update: ENABLED — the agent evolves itself"
else
    echo "  • Daily GitHub self-update: DISABLED (run install_auto_update.sh to enable)"
fi
echo ""
echo "📂 Backup: $PRELIM_HOME.backup.$BACKUP_DATE"
echo "🔄 Rollback: rm -rf \"$PRELIM_HOME\" && mv \"$PRELIM_HOME.backup.$BACKUP_DATE\" \"$PRELIM_HOME\""
echo ""
echo "✨ You're now running Hermes Evolution!"
echo "=========================================="
