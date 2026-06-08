#!/bin/bash
# upgrade.sh v1.6 - Automatic Upgrade Script for Hermes Evolution
# This script does everything automatically - works on any system with Hermes installed
# Source: https://github.com/Lexus2016/hermes-agent-evolution

set -e

echo "🧬 Hermes Evolution Automatic Upgrade v1.6"
echo "=========================================="
echo ""

# Configuration
EVOLUTION_REPO="https://github.com/Lexus2016/hermes-agent-evolution.git"
EVOLUTION_DIR="$HOME/hermes-agent-evolution"
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)

# Detect where Hermes is installed
echo "🔍 Detecting Hermes installation..."
HERMES_PROJECT=$(hermes --version 2>/dev/null | grep "Project:" | cut -d' ' -f2 || echo "")
if [ -z "$HERMES_PROJECT" ]; then
    echo "❌ Cannot detect Hermes installation"
    exit 1
fi
echo "✅ Hermes installed at: $HERMES_PROJECT"

# KEY FIX: Hermes loads skills from ~/.hermes/skills/ NOT from $HERMES_PROJECT/skills/
HERMES_USER_SKILLS="$HOME/.hermes/skills"
HERMES_USER_CRON="$HOME/.hermes/cron"

echo "📂 User skills directory: $HERMES_USER_SKILLS"
echo "📂 User cron directory: $HERMES_USER_CRON"
echo ""

# Step 1: Create backup
echo "📦 Step 1/7: Creating backup..."
if [ -d "$HOME/.hermes" ]; then
    cp -r "$HOME/.hermes" "$HOME/.hermes.backup.$BACKUP_DATE"
    echo "✅ Backup created: ~/.hermes.backup.$BACKUP_DATE"
else
    echo "ℹ️  No existing .hermes found (fresh installation)"
fi

# Step 2: Clean up and clone
echo ""
echo "📥 Step 2/7: Cloning Hermes Evolution..."
rm -rf "$EVOLUTION_DIR" /tmp/hermes-evolution
git clone "$EVOLUTION_REPO" "$EVOLUTION_DIR"
echo "✅ Cloned to: $EVOLUTION_DIR"

# Step 3: Run setup (THIS IS CRITICAL - actually updates Hermes)
echo ""
echo "🔧 Step 3/7: Running setup-hermes.sh (this updates Hermes)..."
cd "$EVOLUTION_DIR"
if [ -f "setup-hermes.sh" ]; then
    bash setup-hermes.sh
    echo "✅ Setup completed"
else
    echo "❌ setup-hermes.sh not found!"
    exit 1
fi

# Step 4: Re-detect Hermes path after setup
echo ""
echo "🔍 Step 4/7: Re-detecting Hermes path after setup..."
HERMES_PROJECT=$(hermes --version 2>/dev/null | grep "Project:" | cut -d' ' -f2 || echo "")
echo "✅ Hermes now at: $HERMES_PROJECT"

# Step 5: Install evolution skills to ~/.hermes/skills/evolution/SKILL_NAME/SKILL.md
echo ""
echo "📚 Step 5/7: Installing evolution skills to: $HERMES_USER_SKILLS"

EVOLUTION_SKILLS="$EVOLUTION_DIR/skills/evolution"

for skill_file in "$EVOLUTION_SKILLS"/*.md; do
    skill_name=$(basename "$skill_file" .md)
    
    # Create skill directory: ~/.hermes/skills/evolution/evolution-research/SKILL.md
    skill_dir="$HERMES_USER_SKILLS/evolution/evolution-$skill_name"
    mkdir -p "$skill_dir"
    
    # Copy skill file as SKILL.md
    cp "$skill_file" "$skill_dir/SKILL.md"
    
    echo "✅ Installed: evolution/evolution-$skill_name"
done

echo ""
echo "📋 Installed evolution skills:"
ls -1d "$HERMES_USER_SKILLS"/evolution/evolution-* 2>/dev/null | while read dir; do
    echo "   - $(basename "$dir")"
done

# Step 6: Copy evolution cron jobs to ~/.hermes/cron/
echo ""
echo "⏰ Step 6/7: Installing evolution cron jobs to: $HERMES_USER_CRON"
EVOLUTION_CRON="$EVOLUTION_DIR/cron/evolution"

if [ -d "$EVOLUTION_CRON" ]; then
    mkdir -p "$HERMES_USER_CRON"
    if [ ! -d "$HERMES_USER_CRON/evolution" ]; then
        cp -r "$EVOLUTION_CRON" "$HERMES_USER_CRON/"
        echo "✅ Evolution cron jobs installed"
    else
        echo "✅ Evolution cron jobs already installed"
    fi
    
    echo "📋 Installed evolution cron jobs:"
    ls -1 "$EVOLUTION_CRON"/*.yaml 2>/dev/null | while read file; do
        echo "   - $(basename "$file" .yaml)"
    done
else
    echo "❌ Evolution cron jobs not found in repository"
fi

# Step 7: Verify installation
echo ""
echo "✅ Step 7/7: Verifying installation..."

if command -v hermes &> /dev/null; then
    echo "✅ Hermes command available"
    
    if hermes skills list 2>/dev/null | grep -q "evolution"; then
        echo "✅ Evolution skills installed and available!"
    else
        echo "⚠️  Evolution skills installed but not yet visible"
        echo "📋 Try: exec bash && hermes skills list | grep evolution"
    fi
else
    echo "❌ Hermes command not found"
    exit 1
fi

echo ""
echo "=========================================="
echo "🎉 Upgrade to Hermes Evolution complete!"
echo ""
echo "📖 What's new:"
echo "  • Evolution skills (research, issues, analysis, implementation, upstream-sync)"
echo "  • Evolution cron jobs (daily research, analysis, implementation)"
echo "  • Self-update capabilities"
echo ""
echo "🔗 Next steps:"
echo "  1. Reload shell: exec bash"
echo "  2. Check skills: hermes skills list | grep evolution"
echo ""
echo "📂 Backup: ~/.hermes.backup.$BACKUP_DATE"
echo "🔄 Rollback: cp -r ~/.hermes.backup.$BACKUP_DATE ~/.hermes"
echo ""
echo "✨ You're now running Hermes Evolution!"
echo "=========================================="
