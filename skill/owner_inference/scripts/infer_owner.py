#!/usr/bin/env python3
"""Infer a suspected owner for a failing test file.

Heuristics (in order of preference):
1. Override map — `.opencode/triage/owner-map.yaml` for explicit mappings.
2. CODEOWNERS — parse GitHub-style CODEOWNERS if present.
3. Git blame — most-frequent author of the failing file.
4. Recent commits — most-frequent author touching the file in the last N commits.
5. Fallback — return `Build` with confidence 0.0.

Usage:
    python3 infer_owner.py '{"repo_path": ".", "file_path": "tests/test_foo.py"}'

Returns JSON: { "assignee": "...", "confidence": 0.0-1.0, "reason": "..." }
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_FALLBACK = "Build"
DEFAULT_CONFIDENCE_THRESHOLD = 0.3
DEFAULT_RECENT_COMMITS = 50


# ---------------------------------------------------------------------------
# Override map (.opencode/triage/owner-map.yaml)
# ---------------------------------------------------------------------------


def load_owner_map(repo_path: str) -> Dict[str, str]:
    """Load the override map if it exists.

    The file is a simple YAML with path-glob -> owner mappings.
    We parse it without requiring PyYAML by handling the simple
    ``key: value`` format.
    """
    map_path = os.path.join(repo_path, ".opencode", "triage", "owner-map.yaml")
    if not os.path.isfile(map_path):
        return {}
    result: Dict[str, str] = {}
    try:
        with open(map_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                result[key.strip().strip('"').strip("'")] = (
                    value.strip().strip('"').strip("'")
                )
    except Exception:
        pass
    return result


def check_owner_map(repo_path: str, file_path: str) -> Optional[Tuple[str, float, str]]:
    """Return (assignee, confidence, reason) if file_path matches an override."""
    owner_map = load_owner_map(repo_path)
    if not owner_map:
        return None
    from fnmatch import fnmatch

    for pattern, owner in owner_map.items():
        if fnmatch(file_path, pattern) or fnmatch(os.path.basename(file_path), pattern):
            return (owner, 1.0, f"owner-map override matched pattern '{pattern}'")
    return None


# ---------------------------------------------------------------------------
# CODEOWNERS
# ---------------------------------------------------------------------------


def parse_codeowners(repo_path: str) -> list:
    """Parse CODEOWNERS file and return list of (pattern, owners) tuples."""
    candidates = [
        os.path.join(repo_path, "CODEOWNERS"),
        os.path.join(repo_path, ".github", "CODEOWNERS"),
        os.path.join(repo_path, "docs", "CODEOWNERS"),
    ]
    codeowners_path = None
    for c in candidates:
        if os.path.isfile(c):
            codeowners_path = c
            break
    if not codeowners_path:
        return []
    rules = []
    try:
        with open(codeowners_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    pattern = parts[0]
                    owners = [p.lstrip("@") for p in parts[1:]]
                    rules.append((pattern, owners))
    except Exception:
        pass
    return rules


def check_codeowners(
    repo_path: str, file_path: str
) -> Optional[Tuple[str, float, str]]:
    """Match file_path against CODEOWNERS rules (last match wins)."""
    rules = parse_codeowners(repo_path)
    if not rules:
        return None
    from fnmatch import fnmatch

    matched_owners = None
    matched_pattern = None
    for pattern, owners in rules:
        # CODEOWNERS patterns: leading / means repo root, otherwise match anywhere
        check_pattern = pattern.lstrip("/")
        if fnmatch(file_path, check_pattern) or fnmatch(
            file_path, "**/" + check_pattern
        ):
            matched_owners = owners
            matched_pattern = pattern
    if matched_owners:
        return (
            matched_owners[0],
            0.8,
            f"CODEOWNERS matched pattern '{matched_pattern}'",
        )
    return None


# ---------------------------------------------------------------------------
# Git blame
# ---------------------------------------------------------------------------


def check_git_blame(repo_path: str, file_path: str) -> Optional[Tuple[str, float, str]]:
    """Use git blame to find the most frequent author of the file."""
    full_path = os.path.join(repo_path, file_path)
    if not os.path.isfile(full_path):
        return None
    try:
        out = subprocess.check_output(
            ["git", "blame", "--porcelain", file_path],
            cwd=repo_path,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    author_counts: Dict[str, int] = {}
    for line in out.splitlines():
        if line.startswith("author "):
            author = line[len("author ") :]
            if author and author != "Not Committed Yet":
                author_counts[author] = author_counts.get(author, 0) + 1

    if not author_counts:
        return None
    total = sum(author_counts.values())
    top_author = max(author_counts, key=author_counts.get)
    confidence = round(author_counts[top_author] / total, 2) if total > 0 else 0.0
    # Scale: blame-based confidence maxes at 0.7
    confidence = min(confidence * 0.7, 0.7)
    return (
        top_author,
        confidence,
        f"git blame: {author_counts[top_author]}/{total} lines authored",
    )


# ---------------------------------------------------------------------------
# Recent commits
# ---------------------------------------------------------------------------


def check_recent_commits(
    repo_path: str, file_path: str, n: int = DEFAULT_RECENT_COMMITS
) -> Optional[Tuple[str, float, str]]:
    """Find the most frequent committer touching file_path in the last n commits."""
    try:
        out = subprocess.check_output(
            ["git", "log", f"-{n}", "--format=%an", "--", file_path],
            cwd=repo_path,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    authors: Dict[str, int] = {}
    for line in out.strip().splitlines():
        line = line.strip()
        if line:
            authors[line] = authors.get(line, 0) + 1

    if not authors:
        return None
    total = sum(authors.values())
    top = max(authors, key=authors.get)
    confidence = round(authors[top] / total, 2) if total > 0 else 0.0
    # Scale: recent-commit confidence maxes at 0.5
    confidence = min(confidence * 0.5, 0.5)
    return (
        top,
        confidence,
        f"recent commits: {authors[top]}/{total} commits in last {n}",
    )


# ---------------------------------------------------------------------------
# Main inference
# ---------------------------------------------------------------------------


def infer_owner(
    repo_path: str,
    file_path: str,
    commit: Optional[str] = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> Dict[str, Any]:
    """Run heuristics in order and return the first result above threshold."""
    heuristics = [
        ("owner_map", lambda: check_owner_map(repo_path, file_path)),
        ("codeowners", lambda: check_codeowners(repo_path, file_path)),
        ("git_blame", lambda: check_git_blame(repo_path, file_path)),
        ("recent_commits", lambda: check_recent_commits(repo_path, file_path)),
    ]
    for name, fn in heuristics:
        try:
            result = fn()
        except Exception:
            continue
        if result is not None:
            assignee, confidence, reason = result
            if confidence >= confidence_threshold:
                return {
                    "assignee": assignee,
                    "confidence": confidence,
                    "reason": reason,
                    "heuristic": name,
                }

    return {
        "assignee": DEFAULT_FALLBACK,
        "confidence": 0.0,
        "reason": "No heuristic matched above threshold; falling back to Build",
        "heuristic": "fallback",
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "expected JSON argument"}))
        sys.exit(2)
    try:
        payload = json.loads(sys.argv[1])
    except Exception:
        print(json.dumps({"error": "invalid JSON"}))
        sys.exit(2)

    repo_path = payload.get("repo_path", ".")
    file_path = payload.get("file_path")
    commit = payload.get("commit")
    threshold = payload.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)

    if not file_path:
        print(json.dumps({"error": "file_path is required"}))
        sys.exit(2)

    result = infer_owner(
        repo_path, file_path, commit=commit, confidence_threshold=threshold
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
