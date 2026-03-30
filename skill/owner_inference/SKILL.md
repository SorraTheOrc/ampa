---
name: owner-inference
description: Infer a suspected owner for a failing test file using CODEOWNERS, git blame, recent commits, and an override map.
---

## Purpose

Provide a deterministic heuristic to identify the likely owner of a failing
test file so that triage can assign it for investigation. Used by the
`check_or_create_critical_issue` triage skill to populate the "suspected owner"
field in new critical issues.

## When to use

- When the triage skill creates a new critical `test-failure` work item and
  needs to assign or suggest an owner.

## Inputs

- JSON payload: `{ "repo_path": ".", "file_path": "tests/test_foo.py", "commit": "abc123" }`
  - `repo_path` — path to the repository root (default `.`)
  - `file_path` — relative path to the failing test file (required)
  - `commit` — optional commit hash for context
  - `confidence_threshold` — minimum confidence to accept a heuristic result (default 0.3)

## Outputs

- JSON: `{ "assignee": "...", "confidence": 0.0-1.0, "reason": "...", "heuristic": "..." }`

## Heuristics (in priority order)

1. **Override map** — `.opencode/triage/owner-map.yaml` for explicit path-to-owner mappings.
2. **CODEOWNERS** — GitHub-style CODEOWNERS file (repo root, `.github/`, or `docs/`).
3. **Git blame** — most frequent author of the failing file by line count.
4. **Recent commits** — most frequent committer touching the file in the last 50 commits.
5. **Fallback** — returns `Build` with confidence 0.0.

## Scripts

- `scripts/infer_owner.py` — CLI entrypoint and library functions.

## Configuration

- Override map: `.opencode/triage/owner-map.yaml`
- Confidence threshold: configurable via `confidence_threshold` in the JSON payload (default 0.3).

## References

- Triage skill: `skill/triage/SKILL.md`
- Runbook: `skill/triage/resources/runbook-test-failure.md`
