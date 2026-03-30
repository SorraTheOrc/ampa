---
name: cleanup
description: "Clean up completed work: inspect branches, update main, remove merged branches (local and optionally remote), and produce a concise report. Trigger on queries like: 'clean up', 'tidy up', 'prune branches', 'housekeeping'."
---

# Cleanup Skill

Triggers

- "clean up"
- "tidy up"
- "cleanup"
- "housekeeping"

## Purpose

Inspect repository branches, identify merged or stale work, remove safely deletable branches, and produce a concise report of actions and next steps.

## Required tools

- `git` (required)
- `gh` (GitHub CLI) — optional for PR summaries

Scripts (implementation)

- The skill ships a set of deterministic scripts under `./scripts/` that implement the non-interactive behaviour described below. Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.

## Preferred execution behaviour (policy)

- By default the agent MUST run the repository's official cleanup scripts listed in this document (for example, `inspect_current_branch.py`, `switch_to_default_and_update.py`, `summarize_branches.py`, `prune_local_branches.py`, `delete_remote_branches.py`). The agent SHOULD NOT substitute its own ad-hoc git commands for these scripts during normal operation.
- The agent may fall back to built-in git inspections or other local checks ONLY in narrowly defined edge cases and only after explicit human instruction. Edge cases include:
  - the expected script is missing or not executable,
  - the script fails with an unexpected error and the user explicitly asks the agent to attempt a local-git fallback,
- The agent MUST refuse to automatically run repository scripts when it detects potentially risky conditions (uncommitted changes, missing scripts, modified scripts) without explicit human confirmation.
- Rationale: preferring the canonical in-repo scripts improves consistency and auditability while the guardrails reduce risk from modified or missing scripts.
- If you offer choices to the user one of those options MUST be to use the audit skill to review the branch in more detail before proceeding. If the user chooses to review with the audit skill, present the report to the user and offer appropriate options for next steps based on that report before proceeding as instructed.

## Preconditions & safety

- Never rewrite history or force-push without explicit permission.
- Default protected branches: `main`, `develop` (do not delete or target for deletion).

## High-level Steps

1. Inspect current branch

Use `skill/cleanup/scripts/inspect_current_branch.py` to inspect the current branch, detect the default branch, fetch `origin --prune` when needed, determine merge status, last commit, unpushed commits, and parse work item token. The agent MUST run this script by default and only perform inline git inspections if an edge case (see "Preferred execution behaviour") applies and the operator approves.

Output a human readable summary of this report using Markdown formatting. IMPORTANT: the agent MUST display the inspection report (or a concise excerpt of it) to the user before presenting any interactive prompts or choices. The displayed report should include at minimum:

- current branch name and default branch
- merge status (merged into default or not)
- uncommitted changes list (git status-style short list)
- unpushed commits count and last commit summary (author, date, sha)
- the path to the full JSON report file when available (e.g. /tmp/cleanup/inspect_current.json)

If the report is large the agent should present a short, human-readable summary and offer to show the full diff or JSON report on demand. Example commands the agent may offer to the user to inspect details locally:

```
# show a short diff of uncommitted changes
git --no-pager diff --name-status

# show the generated JSON report
cat /tmp/cleanup/inspect_current.json
```

If there are no uncommitted or unpushed changes then proceed to step 3.

The agent should NOT proceed without approval when uncommitted changes are present and MUST always display the inspection report before asking the user how to proceed.

Examples:

```bash
python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
```

2. Handle uncommitted and unpushed changes

If the previous step detected uncommitted or unpushed changes, the agent MUST present the inspection report (see step 1) showing those changes and then provide sensible options with a recommendation based on the state (e.g., "Branch has unpushed commits. Would you like to push, stash, or skip?"). The report MUST be visible to the user before any choices are requested.

The presented options must include the option to review the branch with the audit skill before proceeding, and if the user selects that option, the agent should run the audit skill and present the findings to the user before offering next steps.

If the agent is unable to address the uncommitted/unpushed changes through the provided options, it should pause and provide guidance on how to resolve these issues manually before proceeding and stop further.

3. Switch to default branch and update

Only continue with this step if there are no uncommitted or unpushed changes in the current branch.

Run `skill/cleanup/scripts/switch_to_default_and_update.py` to fetch, check out the default branch, and perform a fast-forward pull. The agent MUST run this script by default (see Preferred execution behaviour) and only attempt manual git switch/pull sequences when explicitly instructed by the human in an allowed edge case.

If the pull fails (e.g., due to conflicts), the script will report the issue and you should work with the user to determine how to proceed (e.g., "Default branch cannot be fast-forwarded. Would you like to resolve conflicts manually and retry, or skip updating?"). The agent should NOT attempt to resolve conflicts automatically and should always defer to the human for next steps in this scenario.

Example:

```bash
python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
```

4. Summarize branches and open PRs

Run `skill/cleanup/scripts/summarize_branches.py` to list local branches and include any open PRs targeting the default branch. The agent MUST run this script by default and present the script-generated report, in markdown format, for any deletion decisions.

For branches with unmerged commits or open PRs, present the PR details and skip deletion unless explicitly authorized. The agent should present a clear summary of these branches, including their merge status, last commit, and any associated work items, to help the user make informed decisions about which branches to delete.

Branches that are merged into default, have no unmerged commits and have no open PRs should be listed as candidates for deletion without further permission.

Example:

```bash
python skill/cleanup/scripts/summarize_branches.py --report /tmp/cleanup/branches.json
```

5. Delete local merged branches

Use `skill/cleanup/scripts/prune_local_branches.py` with an explicit branch list derived from the summarize report and user input. The summarize report and user choice are the authoritative source; the prune script only deletes branches you pass in. The agent MUST NOT delete branches outside of the explicit branch list produced by the script and approved by the human.

Example:

```bash
# delete branches identfied by the previous step
python skill/cleanup/scripts/prune_local_branches.py \
  --branches-file /tmp/cleanup/branches_to_delete.json \
  --report /tmp/cleanup/prune_local.json

# Dry-run and produce JSON report
python ./scripts/prune_local_branches.py --dry-run \
  --branches-file /tmp/cleanup/branches_to_delete.json \
  --report /tmp/cleanup/local.json
```

6. Delete remote merged branches

Run `skill/cleanup/scripts/delete_remote_branches.py` — deletes remote branches that are merged into default and older than a threshold (default 14 days). Report on branches deleted, skipped (e.g., due to open PRs), and any errors.

Example:

```bash
# Delete all remote branches merged into default and older than 14 days
python skill/cleanup/scripts/delete_remote_branches.py --days 14 --report /tmp/cleanup/delete_remote.json

# Dry-run mode
python skill/cleanup/scripts/delete_remote_branches.py --days 14 --dry-run --report /tmp/cleanup/delete_remote.json
```

7. Handle edge cases and manual review:

Provide interactive options for handling remaining branches such as rebase, merge, create PR, or assign work item for any remaining branches. Where possible, provide guidance on next steps (e.g., "Branch X is not merged but has no open PR. Would you like to create a PR, rebase onto default, or assign to a work item?").

8. Temporary File Removal

If any temporary files were created (e.g., branch lists, reports), remove them to avoid clutter.

9. Final report

- Produce concise report including:
  - Branches deleted (local + remote)
  - Branches kept and reasons
  - Any operations skipped or requiring manual intervention

Safety prompts (always asked)

- If default branch cannot be fast-forwarded, ask how to proceed (pause or abort).

Outputs

- Human-readable summary printed to terminal.

End.
