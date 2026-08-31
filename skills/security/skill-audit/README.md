# skill-audit — scanner reference

Standing security scan for the Hermes skill store. See `SKILL.md` for the
full model → scan → triage → patch chain; this file is the operator reference
for the bundled scanner.

## Scanner

```bash
python3 scripts/skill_audit_scan.py [--store PATH] [--baseline PATH]
                                    [--allowlist PATH] [--out PATH] [--verbose]
```

| Default | Path |
|---|---|
| store | `$HERMES_HOME/skills` (or `~/.hermes/skills`) |
| baseline | `$HERMES_HOME/state/skill-audit-baseline.json` |
| allowlist | `$HERMES_HOME/state/skill-audit-allowlist.json` (optional) |
| findings | `$HERMES_HOME/state/skill-audit-findings.json` |

**Exit codes**

- `0` — clean: no new/changed/removed/unreviewed findings. Silent (nothing on
  stdout) unless `--verbose`.
- `1` — findings: review needed. Findings JSON written to `--out`, one-line
  summary + per-finding lines on stdout.
- `2` — operational error (bad args, missing store, unwritable baseline).

## Allowlist

A JSON array of skill names (path relative to the store root), e.g.:

```json
["caveman", "security/ai-safe-audit"]
```

Also accepted: `{"skills": ["…"]}`. Semantics:

- Allowlisted **new** skills do not alert on first appearance.
- Allowlisted skills that **change** still alert — "reviewed once" is not
  "reviewed forever".

## Baseline lifecycle

`state/skill-audit-baseline.json` maps every skill file to its SHA-256:

```json
{
  "version": 1,
  "store": "/home/mike/.hermes/skills",
  "scanned_at": "2026-08-31T12:30:00+00:00",
  "skills": {
    "security/ai-safe-audit": {
      "SKILL.md": "9f86d081884c7d65…",
      "references/threat-matrix.md": "60303ae2…"
    }
  }
}
```

The baseline **advances after every successful scan** — each change surfaces
once, then becomes the new known state. Delete the baseline to re-baseline
from scratch (e.g. after a bulk skill migration).

## Findings JSON

`state/skill-audit-findings.json` (written only on findings):

```json
{
  "scan_date": "…",
  "store": "…",
  "baseline": "…",
  "summary": {"new_skills": 0, "changed_skills": 1, "removed_skills": 0, "suspicious_files": 0},
  "allowlisted_skipped": ["caveman"],
  "findings": [
    {
      "type": "changed_skill",
      "skill": "alpha",
      "changes": {"added": [], "modified": ["notes.md"], "removed": []},
      "severity": "medium",
      "action": "review the diff against the baseline — allowlisted skills are not exempt from change review"
    }
  ]
}
```

## Heuristic patterns (scan stage only)

Applied to NEW/ADDED text files only; a hit is a `low`/`high` hint for
triage, never a verdict:

- network fetch piped to a shell (`curl … | sh`, `wget … | bash`)
- base64 decode piped into another command
- `eval(`/`exec(` in python
- `shell=True` in python
- possible hardcoded secrets (`sk-…`, `api_key = "…"`, `token: "…"`)

## Tests

```bash
cd integration/repo && python -m pytest tests/test_skill_audit_scan.py -q
```

Exercises the real CLI end-to-end against temp stores (no mocks): baseline
establishment, change/new/removal detection, allowlist suppression, heuristic
hits, and the exit-2 error path.
