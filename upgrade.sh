#!/bin/bash
# upgrade.sh v2.0 - Automatic Upgrade Script for Hermes Evolution
# Migrates an existing Hermes Agent install onto the Hermes Evolution fork
# WITHOUT data loss. Works on any system with `hermes` already installed.
# Source: https://github.com/Lexus2016/hermes-agent-evolution
#
# v2.0 root-cause rewrite:
#   * Evolution skills are now BUNDLED in the repo under the canonical
#     skills/<category>/<skill>/SKILL.md layout. setup-hermes.sh seeds them
#     into the REAL skills dir via tools/skills_sync.py — no manual copying,
#     no hard-coded ~/.hermes path. This is why skills were "installed but
#     not visible" before: they were dropped into a path Hermes never scans.
#   * HERMES_HOME is resolved by ASKING Hermes, not assumed.
#   * The running gateway is restarted so it reloads the new code + skills.

set -euo pipefail

echo "🧬 Hermes Evolution Automatic Upgrade v2.0"
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

# Step 1: Backup the live Hermes data dir (resolved later; back up the
# conventional location now so we never lose state even if resolution shifts).
echo "📦 Step 1/6: Creating backup..."
PRELIM_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [ -d "$PRELIM_HOME" ]; then
    cp -r "$PRELIM_HOME" "$PRELIM_HOME.backup.$BACKUP_DATE"
    echo "✅ Backup created: $PRELIM_HOME.backup.$BACKUP_DATE"
else
    echo "ℹ️  No existing Hermes data dir at $PRELIM_HOME (fresh install)"
fi

# Step 2: Clean previous attempts and clone the fork
echo ""
echo "📥 Step 2/6: Cloning Hermes Evolution..."
rm -rf "$EVOLUTION_DIR" /tmp/hermes-evolution
git clone --depth 1 "$EVOLUTION_REPO" "$EVOLUTION_DIR"
echo "✅ Cloned to: $EVOLUTION_DIR"

# Step 3: Run setup. This installs the new code AND seeds bundled skills
# (including the evolution/* skills) into the real skills dir via
# tools/skills_sync.py. This is the load-bearing step.
echo ""
echo "🔧 Step 3/6: Running setup-hermes.sh (installs code + seeds skills)..."
cd "$EVOLUTION_DIR"
if [ ! -f "setup-hermes.sh" ]; then
    echo "❌ setup-hermes.sh not found in repo!"
    exit 1
fi
bash setup-hermes.sh
echo "✅ Setup completed"

# Resolve the REAL Hermes home by asking Hermes itself (respects HERMES_HOME,
# profiles, Docker, etc.). Fall back to the conventional path only if that fails.
HERMES_HOME_RESOLVED=$("$EVOLUTION_DIR/venv/bin/python" -c \
    "import sys; sys.path.insert(0, '$EVOLUTION_DIR'); from hermes_constants import get_hermes_home; print(get_hermes_home())" \
    2>/dev/null || echo "${HERMES_HOME:-$HOME/.hermes}")
echo "📂 Hermes home resolved to: $HERMES_HOME_RESOLVED"

# Step 4: Verify evolution skills were seeded into the dir Hermes scans
echo ""
echo "📚 Step 4/6: Verifying evolution skills were seeded..."
SEEDED=$(find "$HERMES_HOME_RESOLVED/skills" -path "*/evolution/*/SKILL.md" 2>/dev/null | wc -l | tr -d ' ')
if [ "$SEEDED" -gt 0 ]; then
    echo "✅ $SEEDED evolution skill(s) present under $HERMES_HOME_RESOLVED/skills/evolution/"
else
    echo "⚠️  No evolution skills found under $HERMES_HOME_RESOLVED/skills/evolution/"
    echo "    (setup-hermes.sh seeding may have skipped them — check tools/skills_sync.py output)"
fi

# Step 5: Restart the gateway so the running process reloads new code + skills.
# A live gateway holds code and the skill cache in memory from when it started;
# updating files on disk does nothing until it restarts.
echo ""
echo "🔄 Step 5/6: Restarting gateway (if running)..."
if hermes gateway status >/dev/null 2>&1; then
    if hermes gateway restart >/dev/null 2>&1; then
        echo "✅ Gateway restarted — now running new code + skills"
    else
        echo "⚠️  Gateway restart failed. Restart manually: hermes gateway restart"
    fi
else
    echo "ℹ️  Gateway not running — it will pick up changes on next start"
fi

# Step 6: Final verification
echo ""
echo "✅ Step 6/6: Verifying installation..."
if hermes skills list 2>/dev/null | grep -qi "evolution"; then
    echo "✅ Evolution skills are visible to Hermes:"
    hermes skills list 2>/dev/null | grep -i "evolution" | sed 's/^/   /'
else
    echo "⚠️  Evolution skills not visible via 'hermes skills list'."
    echo "    Run: hermes skills list   (and inspect $HERMES_HOME_RESOLVED/skills/evolution/)"
fi

# NOTE on cron: evolution cron jobs use a custom YAML format that is NOT the
# same as Hermes' native cron registry (~/.hermes/cron/jobs.json). Copying the
# YAML files does NOT register them. Native registration is a separate,
# deliberate step (a converter evolution.yaml -> jobs.json) — intentionally
# NOT done here to avoid silently claiming jobs are scheduled when they aren't.
echo ""
echo "ℹ️  Evolution cron jobs are NOT auto-registered yet (custom YAML format"
echo "    vs Hermes' jobs.json registry). Schedule them explicitly with"
echo "    'hermes cron add' or the upcoming evolution cron converter."

echo ""
echo "=========================================="
echo "🎉 Upgrade to Hermes Evolution complete!"
echo ""
echo "📖 What's new:"
echo "  • Evolution skills bundled (research, issues, analysis, implementation, upstream-sync)"
echo "  • Self-evolution scaffolding (cron jobs to be registered separately)"
echo ""
echo "📂 Backup: $PRELIM_HOME.backup.$BACKUP_DATE"
echo "🔄 Rollback: rm -rf \"$PRELIM_HOME\" && mv \"$PRELIM_HOME.backup.$BACKUP_DATE\" \"$PRELIM_HOME\""
echo ""
echo "✨ You're now running Hermes Evolution!"
echo "=========================================="
