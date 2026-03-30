#!/usr/bin/env python3
"""check_or_create_critical_issue — triage helper.

Searches Worklog for matching incomplete critical test-failure issues and creates
a new one (using the repository template) when none exists. Returns structured
JSON to stdout.

Matching heuristics (in order of preference):
1. Exact test name match in title + test-failure tag + incomplete status.
2. Title token overlap with test name AND matching stacktrace top-frame.
3. CI job URL or failing commit hash matches an incomplete critical issue.

If multiple candidates match, prefer the most recent. If ambiguity remains,
attach a comment to the most recent candidate and alert triage.
"""

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# WL CLI helpers
# ---------------------------------------------------------------------------


def run_wl(args: List[str]) -> Optional[str]:
    """Run a wl CLI command and return stdout, or None on failure."""
    cmd = ["wl"] + args
    try:
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        return out
    except Exception as exc:
        print(f"[triage] wl command failed: {' '.join(cmd)}: {exc}", file=sys.stderr)
        return None


def list_critical_issues() -> List[Dict[str, Any]]:
    """List all critical issues tagged test-failure."""
    out = run_wl(["list", "--priority", "critical", "--tags", "test-failure", "--json"])
    if not out:
        return []
    try:
        data = json.loads(out)
        # wl list may return {"items": [...]} or a bare list
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def create_issue(title: str, body: str) -> Optional[Dict[str, Any]]:
    """Create a critical test-failure work item via wl create."""
    args = [
        "create",
        "--title",
        title,
        "--description",
        body,
        "--priority",
        "critical",
        "--tags",
        "test-failure",
        "--issue-type",
        "bug",
        "--json",
    ]
    out = run_wl(args)
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def add_comment(issue_id: str, comment: str) -> None:
    """Attach a comment to an existing work item."""
    run_wl(
        [
            "comment",
            "add",
            issue_id,
            "--comment",
            comment,
            "--author",
            "triage-bot",
            "--json",
        ]
    )


# ---------------------------------------------------------------------------
# Telemetry (lightweight: emit JSON events to stderr)
# ---------------------------------------------------------------------------


def emit_event(event_name: str, data: Dict[str, Any]) -> None:
    """Emit a telemetry event to stderr as a JSON line."""
    payload = {"event": event_name, **data}
    print(json.dumps(payload), file=sys.stderr)


# ---------------------------------------------------------------------------
# Owner inference integration
# ---------------------------------------------------------------------------


def infer_owner(repo_path: str, file_path: Optional[str]) -> Dict[str, Any]:
    """Try to infer the owner using the owner-inference skill."""
    if not file_path:
        return {
            "assignee": "Build",
            "confidence": 0.0,
            "reason": "no file path provided",
        }
    try:
        from skill.owner_inference.scripts.infer_owner import infer_owner as _infer

        return _infer(repo_path, file_path)
    except Exception:
        return {
            "assignee": "Build",
            "confidence": 0.0,
            "reason": "owner inference unavailable",
        }


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def render_template(
    test_name: str,
    stdout_excerpt: str,
    stack_trace: str,
    commit_hash: Optional[str],
    ci_url: Optional[str],
    owner_info: Dict[str, Any],
) -> str:
    """Render the test-failure template with provided evidence."""
    commit_line = commit_hash or "(not available)"
    ci_line = ci_url or "(not available)"
    owner_line = f"{owner_info.get('assignee', 'Build')} (confidence: {owner_info.get('confidence', 0.0)}, reason: {owner_info.get('reason', 'unknown')})"

    # Truncate stdout excerpt to 1k characters per template spec
    excerpt = stdout_excerpt[:1000] if stdout_excerpt else "(no output captured)"

    checkout_step = (
        f"git checkout {commit_hash}" if commit_hash else "git checkout <commit-hash>"
    )

    return f"""## Failure Signature

- Test name: {test_name}
- Failing commit: {commit_line}
- CI job: {ci_line}

## Evidence

- Short stderr/stdout excerpt (first 1k characters):

```
{excerpt}
```

## Steps To Reproduce

1. Checkout the commit: `{checkout_step}`
2. Run the failing test: `pytest -k "{test_name}" -q` (or equivalent command)
3. Capture full logs and attach to the work item

## Impact

Failing test detected by agent during automated run. May block PR creation for the agent's current work item.

## Suggested Triage Steps

1. Verify flakiness: rerun CI/test locally once.
2. If reproducible, add owner from owner-inference heuristics and assign for triage.
3. If flaky, tag `flaky` and route to flaky-test queue.

## Suspected Owner

{owner_line}

## Links

- Runbook: skill/triage/resources/runbook-test-failure.md
- CI artifacts: {ci_line}
"""


# ---------------------------------------------------------------------------
# Matching heuristics
# ---------------------------------------------------------------------------


def _get_status(item: Dict[str, Any]) -> str:
    """Extract status from a work item dict (handles nested workItem)."""
    status = item.get("status") or (item.get("workItem") or {}).get("status", "")
    return status.lower().replace("-", "_")


def _get_id(item: Dict[str, Any]) -> Optional[str]:
    """Extract the work item id."""
    return item.get("id") or (item.get("workItem") or {}).get("id")


def _get_field(item: Dict[str, Any], field: str) -> str:
    """Extract a string field from item or nested workItem."""
    return item.get(field, "") or (item.get("workItem") or {}).get(field, "") or ""


def _is_incomplete(item: Dict[str, Any]) -> bool:
    """True if the work item is open or in_progress."""
    return _get_status(item) in ("open", "in_progress")


def _updated_at(item: Dict[str, Any]) -> str:
    """Return updatedAt for sorting (most recent first)."""
    return _get_field(item, "updatedAt") or _get_field(item, "createdAt") or ""


def _tokenize(text: str) -> set:
    """Split text into lowercase alphanumeric tokens (split on underscores too)."""
    # First extract word-like sequences, then split on underscores
    raw = re.findall(r"[a-z0-9_]+", text.lower())
    tokens = set()
    for r in raw:
        for part in r.split("_"):
            if part:
                tokens.add(part)
    return tokens


def _extract_top_frame(stack_trace: str) -> Optional[str]:
    """Extract the top frame filename from a stack trace (Python-style)."""
    # Match  File "path/to/file.py", line N  or similar
    match = re.search(r'File "([^"]+)"', stack_trace)
    if match:
        return match.group(1)
    # Fallback: first line that looks like a file path
    for line in stack_trace.splitlines():
        m = re.search(r"([a-zA-Z0-9_/\\.-]+\.(py|js|ts|go|rs))", line)
        if m:
            return m.group(1)
    return None


def match_heuristic_1(candidates: List[Dict], test_name: str) -> Optional[Dict]:
    """Heuristic 1: Exact test name match in title or body."""
    matches = []
    for c in candidates:
        if not _is_incomplete(c):
            continue
        title = _get_field(c, "title")
        body = _get_field(c, "description")
        if test_name and (test_name in title or test_name in body):
            matches.append(c)
    if not matches:
        return None
    # Prefer most recent
    matches.sort(key=_updated_at, reverse=True)
    return matches[0]


def match_heuristic_2(
    candidates: List[Dict], test_name: str, stack_trace: str
) -> Optional[Dict]:
    """Heuristic 2: Title token overlap + matching stacktrace top-frame."""
    if not stack_trace:
        return None
    top_frame = _extract_top_frame(stack_trace)
    if not top_frame:
        return None
    test_tokens = _tokenize(test_name) if test_name else set()
    if not test_tokens:
        return None

    matches = []
    for c in candidates:
        if not _is_incomplete(c):
            continue
        title_tokens = _tokenize(_get_field(c, "title"))
        overlap = test_tokens & title_tokens
        if len(overlap) < max(1, len(test_tokens) * 0.5):
            continue
        # Check if top frame appears in body
        body = _get_field(c, "description")
        if top_frame in body:
            matches.append(c)

    if not matches:
        return None
    matches.sort(key=_updated_at, reverse=True)
    return matches[0]


def match_heuristic_3(
    candidates: List[Dict], commit_hash: Optional[str], ci_url: Optional[str]
) -> Optional[Dict]:
    """Heuristic 3: CI job URL or failing commit hash match."""
    if not commit_hash and not ci_url:
        return None

    matches = []
    for c in candidates:
        if not _is_incomplete(c):
            continue
        body = _get_field(c, "description")
        title = _get_field(c, "title")
        text = title + " " + body
        if commit_hash and commit_hash in text:
            matches.append(c)
        elif ci_url and ci_url in text:
            matches.append(c)

    if not matches:
        return None
    matches.sort(key=_updated_at, reverse=True)
    return matches[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def check_or_create(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Core logic: search for or create a critical test-failure issue.

    Returns a structured dict with issueId, created, matchedId, reason.
    """
    # Parse input — support both flat and nested failure_signature formats
    sig = payload.get("failure_signature", {})
    test_name = payload.get("test_name") or sig.get("test_name")
    stdout_excerpt = payload.get("stdout_excerpt") or sig.get("stdout_excerpt", "")
    stack_trace = payload.get("stack_trace") or sig.get("stack_trace", "")
    commit_hash = payload.get("commit_hash") or sig.get("commit_hash")
    ci_url = payload.get("ci_url") or sig.get("ci_url")
    repo_path = payload.get("repo_path", ".")
    file_path = payload.get("file_path") or sig.get("file_path")

    if not test_name:
        return {"error": "test_name is required"}

    # Fetch candidates
    candidates = list_critical_issues()

    # Run heuristics in order
    heuristic_name = None
    match = match_heuristic_1(candidates, test_name)
    if match:
        heuristic_name = "exact_test_name"
    if not match:
        match = match_heuristic_2(candidates, test_name, stack_trace)
        if match:
            heuristic_name = "token_overlap_stacktrace"
    if not match:
        match = match_heuristic_3(candidates, commit_hash, ci_url)
        if match:
            heuristic_name = "commit_or_ci_url"

    if match:
        issue_id = _get_id(match)
        # Enhance existing issue with new evidence
        comment_lines = ["Additional evidence from triage helper:"]
        if stdout_excerpt:
            comment_lines += [
                "",
                "stdout excerpt:",
                "```",
                stdout_excerpt[:1000],
                "```",
            ]
        if stack_trace:
            comment_lines += ["", "Stack trace:", "```", stack_trace, "```"]
        if commit_hash:
            comment_lines.append(f"\nFailing commit: {commit_hash}")
        add_comment(issue_id, "\n".join(comment_lines))
        emit_event(
            "triage.issue.enhanced", {"issueId": issue_id, "heuristic": heuristic_name}
        )
        return {
            "issueId": issue_id,
            "created": False,
            "matchedId": issue_id,
            "reason": f"matched_existing ({heuristic_name})",
        }

    # No match — create a new issue using the template
    owner_info = infer_owner(repo_path, file_path)

    title = f"[test-failure] {test_name} — failing test"
    body = render_template(
        test_name=test_name,
        stdout_excerpt=stdout_excerpt,
        stack_trace=stack_trace,
        commit_hash=commit_hash,
        ci_url=ci_url,
        owner_info=owner_info,
    )

    created = create_issue(title, body)
    if not created:
        return {"error": "failed to create issue via wl create"}

    new_id = None
    if isinstance(created, dict):
        new_id = created.get("id") or (created.get("workItem") or {}).get("id")

    emit_event("triage.issue.created", {"issueId": new_id, "testName": test_name})
    return {"issueId": new_id, "created": True, "reason": "created_new"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "expected JSON argument"}))
        sys.exit(2)
    try:
        payload = json.loads(sys.argv[1])
    except Exception:
        print(json.dumps({"error": "invalid JSON"}))
        sys.exit(2)

    result = check_or_create(payload)
    print(json.dumps(result))
    if "error" in result:
        sys.exit(2)


if __name__ == "__main__":
    main()
