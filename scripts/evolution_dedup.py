#!/usr/bin/env python3
"""
Evolution dedup cache — O(1) check + record for issue ideas.

Usage:
    python scripts/evolution_dedup.py check "<title>"
        → exit 0 if NOT seen before (cache miss, proceed to gh query)
        → exit 1 if already seen (cache hit, skip it)

    python scripts/evolution_dedup.py record "<title>" <status> <issue_num> <date>
        → status: "filed" or "considered"
        → issue_num: GitHub issue number (or "" for "considered")
        → Stores in ~/.hermes/evolution/dedup-cache.json
"""

import json
import sys
import os
import hashlib

CACHE_PATH = os.path.expanduser("~/.hermes/evolution/dedup-cache.json")


def _load():
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, "r") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _key(title):
    """Normalize title to a stable key for dedup."""
    t = title.strip().lower()
    # remove common prefixes
    for p in ["[feature] ", "[fix] ", "[improvement] ", "[ux] "]:
        if t.startswith(p):
            t = t[len(p):]
            break
    # collapse whitespace
    t = " ".join(t.split())
    # hash to keep the cache file small
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def cmd_check(title):
    data = _load()
    k = _key(title)
    if k in data:
        sys.stderr.write(f"CACHE HIT: '{title}' -> {data[k]}\n")
        sys.exit(1)
    sys.stdout.write(f"CACHE MISS: '{title}'\n")
    sys.exit(0)


def cmd_record(title, status, issue_num, date):
    data = _load()
    k = _key(title)
    data[k] = {
        "title": title,
        "status": status,
        "issue": issue_num if issue_num else None,
        "date": date,
    }
    _save(data)
    sys.stdout.write(f"Recorded: '{title}' -> {status}\n")
    sys.exit(0)


def cmd_list():
    data = _load()
    for k, v in sorted(data.items()):
        print(f"{k}\t{v['title']}\t{v['status']}\t{v.get('issue', '')}\t{v.get('date', '')}")
    if not data:
        print("(empty)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dedup.py <check|record|list> [...]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        if len(sys.argv) < 3:
            print("Usage: dedup.py check <title>", file=sys.stderr)
            sys.exit(1)
        cmd_check(sys.argv[2])

    elif command == "record":
        if len(sys.argv) < 6:
            print("Usage: dedup.py record <title> <status> <issue_num> <date>", file=sys.stderr)
            sys.exit(1)
        cmd_record(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])

    elif command == "list":
        cmd_list()

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)
