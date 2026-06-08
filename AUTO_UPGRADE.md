# 🧬 Hermes Evolution — Upgrade & Install Instructions

**Upgrade an existing Hermes Agent install to Hermes Evolution without data loss.**

---

## 🎯 What This Does

Upgrades your existing Hermes Agent installation to **Hermes Evolution** — a
self-improving fork with autonomous research, proposal generation, and
self-update capabilities.

**Zero data loss:** profiles, skills, cron jobs, memories, and configuration
are preserved (a timestamped backup is taken automatically).

---

## 📋 Prerequisites

- ✅ Python 3.11+
- ✅ Git installed
- ✅ Active internet connection
- ✅ Existing Hermes Agent installation (`hermes` on PATH)

---

## 🚀 Recommended: One-Command Upgrade

```bash
# Clone fresh (git always gets the latest main — see the cache note below)
rm -rf ~/hermes-agent-evolution /tmp/hermes-evolution
git clone https://github.com/Lexus2016/hermes-agent-evolution.git ~/hermes-agent-evolution

# Run the upgrade (backup → setup → seed skills → register cron → restart gateway)
bash ~/hermes-agent-evolution/upgrade.sh

# Verify
hermes skills list | grep -i evolution
hermes cron list   | grep -i evolution
```

> **⚠️ Do not pipe the script from a CDN** (`curl … raw.githubusercontent.com`
> or `jsdelivr`). Those cache the script aggressively (jsDelivr `@main` up to
> 7 days), so `curl | bash` can run a STALE `upgrade.sh`. `git clone` always
> pulls the current `main`.

### What `upgrade.sh` does (7 steps)

1. **Backup** the live Hermes data dir (`$HERMES_HOME` or `~/.hermes`).
2. **Clone** the fork to `~/hermes-agent-evolution`.
3. **Run `setup-hermes.sh`** — installs new code AND seeds bundled skills
   (including `evolution/*`) into the real skills dir via `tools/skills_sync.py`.
4. **Verify** evolution skills landed in the dir Hermes actually scans.
5. **Register evolution cron jobs** into Hermes' native `jobs.json` registry
   (via `scripts/register_evolution_cron.py`, idempotent by job name).
6. **Restart the gateway** so the running process reloads new code + skills.
   Opt out with `--no-restart` or `HERMES_SKIP_GATEWAY_RESTART=1`.
7. **Verify** skills and cron jobs are visible to Hermes.

```bash
# Skip the gateway restart (e.g. to drain active sessions yourself first)
bash ~/hermes-agent-evolution/upgrade.sh --no-restart
# ...then apply when ready:
hermes gateway restart
```

---

## 🧩 Why a restart is required

A running gateway loads code and caches the skill list **in memory at start**.
Updating files on disk changes nothing until the process restarts:

- **Skills only** (hot): inside the gateway run `/reload-skills` (rescans the
  skills dir, no process restart).
- **New code / new version**: a full `hermes gateway restart` is required —
  reload does not reload Python modules.

`hermes gateway restart` is restart-aware: from a shell it does a graceful
drain-restart; from within the gateway (self-update) it requests an async
SIGUSR1 self-restart, so it never kills itself mid-script.

---

## ⏰ Cron jobs (important)

Hermes schedules jobs ONLY from its native registry `~/.hermes/cron/jobs.json`
(see `cron/jobs.py`). The evolution jobs ship as rich custom YAML under
`cron/evolution/*.yaml` — **copying those files does not schedule anything.**
`upgrade.sh` registers them for you; to (re-)run it standalone:

```bash
# Preview without writing:
~/hermes-agent-evolution/venv/bin/python \
    ~/hermes-agent-evolution/scripts/register_evolution_cron.py --dry-run

# Register (idempotent — safe to re-run):
~/hermes-agent-evolution/venv/bin/python \
    ~/hermes-agent-evolution/scripts/register_evolution_cron.py

hermes cron list | grep -i evolution
```

---

## 🔐 Configure Evolution (GitHub access)

Evolution's research/issue/PR jobs need GitHub access. **Use a dedicated
fine-grained Personal Access Token, not your personal/classic token:**

- **Repository access:** only `Lexus2016/hermes-agent-evolution`
- **Permissions:** Contents (RW), Pull requests (RW), Issues (RW) — nothing else

```bash
# PUBLIC mode (research + proposals): read/PR/issues scope is enough
export GITHUB_TOKEN="<fine-grained-pat>"

# PRIVATE mode (owner only: implementation + self-update)
export GITHUB_PRIVATE_TOKEN="<fine-grained-pat>"
```

> **Security:** Do NOT hard-code tokens into `~/.bashrc` in plaintext or into
> any git remote URL. Prefer a secrets manager / env file with `chmod 600`,
> or the Hermes secrets vault. A leaked broad-scope token gives an attacker
> (or a prompt-injected agent) the keys to your repositories.

---

## ✅ Verification

```bash
hermes profile list                 # profiles preserved
ls "$(hermes --version 2>/dev/null | grep Project: | cut -d' ' -f2)" >/dev/null 2>&1 || true
hermes skills list | grep -i evolution
hermes cron list   | grep -i evolution

# Explicitly load an evolution skill (canonical names use a hyphen):
hermes --skill evolution-research "What's new in AI agents?"
```

---

## 🔄 Rollback

`upgrade.sh` prints the exact rollback command with your backup path. Generic form:

```bash
ls -d "${HERMES_HOME:-$HOME/.hermes}".backup.* 2>/dev/null   # find backups
# Restore (replace TIMESTAMP):
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
rm -rf "$HOME_DIR" && mv "$HOME_DIR.backup.TIMESTAMP" "$HOME_DIR"
hermes gateway restart
```

A scripted migration/verify/rollback path also exists under `scripts/`
(`migrate-from-hermes.sh`, `verify-migration.py`, `rollback-migration.py`)
for advanced, step-by-step control.

---

## 📚 What's New

### Evolution Skills (canonical names)
- **evolution-research** — research other AI agents and papers
- **evolution-issues** — create GitHub issues with proposals
- **evolution-analysis** — analyze and prioritize improvements
- **evolution-implementation** — implement and self-update
- **evolution-upstream-sync** — sync with upstream Hermes Agent

### Automated Cron Jobs (registered in the native scheduler)
- Research — daily 09:00
- Issue creation — daily 12:00
- Analysis — daily 21:00 (PRIVATE mode)
- Implementation — daily 22:00 (PRIVATE mode)
- Upstream sync — weekly (PRIVATE mode)

---

## 🎯 How Evolution Works

### PUBLIC Mode (all installations)
- ✅ Research other agents and papers
- ✅ Create GitHub issues with improvement proposals
- ❌ Cannot modify code or self-update

### PRIVATE Mode (repository owner only)
- ✅ Everything in PUBLIC mode, plus:
- ✅ Analyze and prioritize proposals
- ✅ Implement selected improvements
- ✅ Create versions and self-update (with tests + rollback safeguards)
- ✅ Sync with upstream Hermes Agent

---

## 📖 More Information

- **Repository**: https://github.com/Lexus2016/hermes-agent-evolution
- **Evolution docs**: `EVOLUTION_README.md`
- **Upstream**: https://github.com/nousresearch/hermes-agent

---

**Welcome to Hermes Evolution!** 🧬🚀 Your data is backed up, and you can roll
back at any time.
