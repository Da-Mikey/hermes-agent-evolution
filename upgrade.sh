#!/bin/bash
# REAL Automatic Upgrade Script: Hermes Agent → Hermes Evolution
# This script DOES EVERYTHING automatically - no manual steps required

set -e

echo "🧬 Automatic Upgrade to Hermes Evolution"
echo "=========================================="
echo ""

# Configuration
EVOLUTION_REPO="https://github.com/Lexus2016/hermes-agent-evolution.git"
EVOLUTION_DIR="$HOME/hermes-agent-evolution"
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)

# Step 1: Create backup
echo "📦 Step 1/6: Creating backup..."
if [ -d "$HOME/.hermes" ]; then
    cp -r "$HOME/.hermes" "$HOME/.hermes.backup.$BACKUP_DATE"
    echo "✅ Backup created: ~/.hermes.backup.$BACKUP_DATE"
else
    echo "ℹ️  No existing .hermes found (fresh installation)"
fi

# Step 2: Clean up and clone
echo ""
echo "📥 Step 2/6: Cloning Hermes Evolution..."
rm -rf "$EVOLUTION_DIR" /tmp/hermes-evolution
git clone "$EVOLUTION_REPO" "$EVOLUTION_DIR"
echo "✅ Cloned to: $EVOLUTION_DIR"

# Step 3: Run setup (THIS IS CRITICAL - actually updates Hermes)
echo ""
echo "🔧 Step 3/6: Running setup-hermes.sh (this updates Hermes)..."
cd "$EVOLUTION_DIR"
if [ -f "setup-hermes.sh" ]; then
    bash setup-hermes.sh
    echo "✅ Setup completed"
else
    echo "❌ setup-hermes.sh not found!"
    exit 1
fi

# Step 4: Copy evolution skills
echo ""
echo "📚 Step 4/6: Installing evolution skills..."
EVOLUTION_SKILLS="$EVOLUTION_DIR/skills/evolution"
HERMES_SKILLS="$HOME/.hermes/skills"

if [ -d "$EVOLUTION_SKILLS" ]; then
    mkdir -p "$HERMES_SKILLS"
    cp -r "$EVOLUTION_SKILLS" "$HERMES_SKILLS/"
    echo "✅ Evolution skills installed"
    
    # List installed skills
    echo "📋 Installed evolution skills:"
    ls -1 "$EVOLUTION_SKILLS"/*.md 2>/dev/null | while read file; do
        echo "   - $(basename $file .md)"
    done
else
    echo "❌ Evolution skills not found in repository"
fi

# Step 5: Copy evolution cron jobs
echo ""
echo "⏰ Step 5/6: Installing evolution cron jobs..."
EVOLUTION_CRON="$EVOLUTION_DIR/cron/evolution"
HERMES_CRON="$HOME/.hermes/cron"

if [ -d "$EVOLUTION_CRON" ]; then
    mkdir -p "$HERMES_CRON"
    cp -r "$EVOLUTION_CRON" "$HERMES_CRON/"
    echo "✅ Evolution cron jobs installed"
    
    # List installed cron jobs
    echo "📋 Installed evolution cron jobs:"
    ls -1 "$EVOLUTION_CRON"/*.yaml 2>/dev/null | while read file; do
        echo "   - $(basename $file .yaml)"
    done
else
    echo "❌ Evolution cron jobs not found in repository"
fi

# Step 6: Verify installation
echo ""
echo "✅ Step 6/6: Verifying installation..."

# Check if hermes command exists
if command -v hermes &> /dev/null; then
    echo "✅ Hermes command available"
    
    # Check evolution skills
    if hermes skills list 2>/dev/null | grep -q "evolution"; then
        echo "✅ Evolution skills installed and available"
    else
        echo "⚠️  Evolution skills installed but not yet visible (may need to reload shell)"
    fi
else
    echo "❌ Hermes command not found - something went wrong"
    exit 1
fi

echo ""
echo "=========================================="
echo "🎉 Upgrade to Hermes Evolution complete!"
echo ""
echo "📖 What's new:"
echo "  • Evolution skills (research, issues, analysis, implementation)"
echo "  • Evolution cron jobs (daily research, analysis, implementation)"
echo "  • Self-update capabilities"
echo ""
echo "🔗 Next steps:"
echo "  1. Test: hermes --help"
echo "  2. Check skills: hermes skills list | grep evolution"
echo "  3. Read docs: cat $EVOLUTION_DIR/EVOLUTION_README.md"
echo ""
echo "📂 Backup location: ~/.hermes.backup.$BACKUP_DATE"
echo "🔄 Rollback if needed: cp -r ~/.hermes.backup.$BACKUP_DATE ~/.hermes"
echo ""
echo "✨ You're now running Hermes Evolution!"
echo "=========================================="
