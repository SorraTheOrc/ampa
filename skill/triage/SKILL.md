---
name: triage
description: Triage workflows and helpers for test-failure detection and critical issue creation. Provides a skill to search for or create critical `test-failure` work items and related resources.
---

Purpose
-------
Provide a deterministic helper for agents that detect failing tests they do not own. The skill's canonical function is `check_or_create_critical_issue` which searches Worklog for matching incomplete critical issues and creates a new one using the repository template when none exists.

When to use
-----------
- When an agent observes a failing test during implementation that appears to originate outside of the agent's current change set.

Inputs
------
- JSON payload (flat or nested under `failure_signature`):
  - `test_name` (required): name of the failing test
  - `stdout_excerpt`: captured test output
  - `stack_trace`: full stack trace
  - `commit_hash`: failing commit hash
  - `ci_url`: CI job URL
  - `repo_path`: repository root for owner inference (default `.`)
  - `file_path`: path to the failing test file (for owner inference)

Outputs
-------
- A JSON object: `{ issueId, created: true|false, matchedId?: id, reason: string }`

References
----------
- Templates: `skill/triage/resources/test-failure-template.md`
- Runbook: `skill/triage/resources/runbook-test-failure.md`
- Owner inference: `skill/owner_inference/SKILL.md`

Scripts
-------
- `scripts/check_or_create.py` — implementation using `wl` CLI.

Matching Heuristics
-------------------
Heuristics are applied in order of preference:
1. **Exact test name match** — test name appears in title or body of an incomplete `test-failure` issue.
2. **Token overlap + stacktrace** — title shares significant tokens with test name AND the stacktrace top-frame appears in the issue body.
3. **Commit hash or CI URL** — commit hash or CI URL appears in an incomplete `test-failure` issue.

If multiple candidates match, the most recently updated is preferred.

Behavior
--------
- Prefer conservative matches: if any incomplete (open or in_progress) `test-failure` issue matches via the heuristics above, return the existing issue id.
- If no match is found, create a new `critical` work item using the template (with all required sections), infer the suspected owner via the owner-inference skill, and return the new id.
- When enhancing an existing issue, do not overwrite existing fields — add a comment with new evidence instead.

Telemetry
---------
- Emits JSON events to stderr: `triage.issue.created`, `triage.issue.enhanced`.

Examples
--------
Calling the script with a JSON payload should return the structured result and print JSON to stdout.
