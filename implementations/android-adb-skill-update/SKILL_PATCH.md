---
title: "android-adb skill — Add FRP/Samsung bypass patterns from 2026-06-27 session"
source: introspection 2026-06-28, finding #3
target_skill: ~/.hermes/skills/hardware/android-adb/SKILL.md
date: 2026-06-28
status: proposed
---

# Skill Update: android-adb — FRP/Samsung Bypass Patterns

## Source
FRP bypass session 2026-06-27 (SM-X205 Samsung Galaxy Tab A8, Unisoc T610).
The agent loaded the `android-adb` skill but it lacked specific Samsung Unisoc
FRP patterns, causing the agent to try methods known not to work (Heimdall flash,
fastboot, ADB sideload).

Much of this knowledge already exists in
`~/.hermes/skills/hardware/android-adb/references/samsung-frp-bypass.md`,
but the main `SKILL.md` should surface the key patterns so the agent doesn't
need to discover the reference file to avoid known-broken approaches.

## Changes Proposed

### 1. Add to "Common blocked-scenario patterns" section (line ~63)

After the existing "FRP/Activation lock" bullet, add a new sub-bullet:

```markdown
- **Samsung Unisoc FRP (SM-X200/X205, Tab A8):** These tablets use Unisoc T610/T618
  chipsets, NOT Exynos or Qualcomm. Standard Samsung methods fail:
  - Heimdall v2.0.2 only speaks Samsung bootloader v1; SM-X205 uses v2/v4
  - Fastboot is NOT supported on Unisoc — `fastboot devices` hangs
  - ADB sideload in recovery only accepts signed OTA packages
  - Recovery "Apply update from ADB" shows `state: sideload` — `adb shell` returns
    `error: closed`
  - ✅ Working: SamFw FRP Tool (Windows), SUT Tool (Windows), Odin flash (CSC wipes FRP),
    odin4 (Linux flashing), paid tools (Chimera, NCK, EFT Pro)
  - See `references/samsung-frp-bypass.md` for full details
```

### 2. Add model identification tip to "Device info" section (line ~420)

After the existing `getprop ro.product.model` line, emphasize it for FRP:

```markdown
### FRP Device Identification (critical first step)

Before attempting any FRP bypass, identify the exact model and chipset:

```bash
adb shell getprop ro.product.model     # e.g., SM-X205
adb shell getprop ro.product.board     # chipset platform
adb shell getprop ro.hardware          # hardware platform (e.g., u616 for Unisoc)
adb shell getprop ro.build.fingerprint # full build info
```

**Model misidentification is costly.** SM-X205 (Unisoc) is often confused with
SM-T220/T225 (MediaTek Tab A7 Lite). If the model number isn't confirmed,
the agent will attempt wrong chipset methods and waste 20+ turns.
```

### 3. Update references section

The existing references section already points to `samsung-frp-bypass.md`. Keep
this reference. Optionally add a note that this is the primary source for
Samsung FRP patterns.

### 4. Add cross-reference to windows-remote skill

In the existing note "All FRP removal tools require Windows", add a see-also:

```markdown
> **⚠️ All FRP removal tools require Windows.** The agent CANNOT run them via
> `terminal` (Linux). Instruct the user to run tools locally. See the
> `windows-remote` skill for the correct interaction pattern.
```

## Implementation Notes

The target file is `~/.hermes/skills/hardware/android-adb/SKILL.md` (783 lines).
The changes are additive — they do not remove any existing content.

### Git commands that would be needed (NOT to be executed):

```bash
# Branch creation (would be needed)
# cd ~/.hermes/evolution
# git checkout -b feat/android-adb-frp-patterns

# After editing SKILL.md:
# git add implementations/android-adb-skill-update/SKILL_PATCH.md
# git commit -m "feat(android-adb): add Samsung Unisoc FRP bypass patterns from 2026-06-27 session"

# PR creation (would be needed)
# gh pr create --title "android-adb: Add FRP/Samsung bypass patterns" \
#   --body "Adds SM-X205 identification, Heimdall vs Odin, Test Point + EDL, \
#     SamFw FRP Tool, and recovery navigation pitfalls from the 2026-06-27 FRP session."
```

## Validation

- [ ] Main SKILL.md updated with the new FRP patterns
- [ ] Cross-reference to `windows-remote` skill added where FRP tools require Windows
- [ ] `references/samsung-frp-bypass.md` remains as the detailed reference (no changes needed — already comprehensive)
