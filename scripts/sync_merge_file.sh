#!/usr/bin/env bash
# 3-way merge one deferred file from the upstream catch-up (phase 2 of #1526).
#
# Phase 1 froze every conflicting file at our pre-sync version. This replays the
# real 3-way merge for one of them so the conflicts can be resolved by hand:
#
#   base    = v2026.7.20   (our merge-base — the last common ancestor)
#   ours    = origin/main  (our pre-sync version, with the fork's edits)
#   theirs  = v2026.7.30   (the upstream release being synced to)
#
# Writes the merged result into the working tree with conflict markers, and
# prints how many are left to resolve. Our fork delta for the file is printed
# first, so you know what has to survive.
set -euo pipefail

FILE="${1:?usage: sync_merge_file.sh <path>}"
BASE_REF="${BASE_REF:-v2026.7.20}"
OURS_REF="${OURS_REF:-origin/main}"
THEIRS_REF="${THEIRS_REF:-v2026.7.30}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git show "$BASE_REF:$FILE"   > "$TMP/base"   2>/dev/null || : > "$TMP/base"
git show "$OURS_REF:$FILE"   > "$TMP/ours"   2>/dev/null || : > "$TMP/ours"
git show "$THEIRS_REF:$FILE" > "$TMP/theirs" 2>/dev/null || {
    echo "note: upstream deleted $FILE at $THEIRS_REF"
    : > "$TMP/theirs"
}

echo "=== fork delta that must survive ($BASE_REF..$OURS_REF) ==="
git diff --stat "$BASE_REF" "$OURS_REF" -- "$FILE" | tail -1

cp "$TMP/ours" "$TMP/merged"
set +e
git merge-file -L ours -L base -L upstream "$TMP/merged" "$TMP/base" "$TMP/theirs"
set -e

mkdir -p "$(dirname "$FILE")"
cp "$TMP/merged" "$FILE"

CONFLICTS=$(grep -c '^<<<<<<< ours' "$FILE" || true)
echo "=== $FILE: ${CONFLICTS:-0} conflict(s) to resolve ==="
grep -n '^<<<<<<< ours' "$FILE" || true
