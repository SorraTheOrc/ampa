---
name: implement
description: Implement a Worklog work item by writing code, tests and documentation to meet acceptance criteria, following a deterministic workflow. Trigger on user queries such as: 'Implement <work-item-id>', 'Complete <work-item-id>', 'Work on <work-item-id>'.
---

## Purpose

Provide a deterministic, step-by-step implementation workflow for completing a
Worklog work item thorugh the creation of code, tests, and documentation.

## Inputs

- work-item id: required. Validate id format `<prefix>-<hash>` and prompt if
  missing.
- Optional freeform guidance in the arguments string may be used to shape the
  implementation approach.

## Outputs

- Tests and implementation code meeting acceptance criteria (committed to a
  branch and pushed to origin).
- Pull Request URL and work item comments referencing the PR and summarising
  work.

## References to Bundled Resources

- Intake/interview helpers: `.opencode/command/intake.md`,
  `.opencode/command/plan.md`.

Security note: Do not push or create PRs automatically unless the invoking
agent has explicit permission to push to the repository and open pull
requests. Require explicit confirmation before performing remote actions
(push/pr creation) when operating without an operator-approved credential.
When in doubt, produce the exact `git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid including secrets, tokens, or personally-identifiable data
in work item comments or PR bodies. If such data must be referenced, reference
it by work-item id or document path instead of pasting values. Mask or redact
any sensitive values before writing them to logs or comments.

## Best Practices

- Follow the steps in order and do not skip steps.
- Keep implementation focused on meeting acceptance criteria with minimal changes.
- Use work item comments to document your process, decisions, and next steps.
- Handle errors gracefully and provide actionable messages for remediation.
- If the work item is not well-defined, do not proceed with implementation. Instead, run the intake interview to clarify and update the work item before implementing.
- If the work item has blockers or dependencies, implement those first before proceeding with the main work item.
- Never commit directly to `main`. Always create a feature or bug branch for implementation.
- When creating branches, include the work item id in the branch name for traceability (e.g., `feature/WL-123-add-auth`).
- When creating a commit message, review the diff and write a concise message summarizing the changes made and the reason for the change, referencing the work item id.
- When committing add a comment to the work item with the commit message and hash.
- Only create a PR when all acceptance criteria have been met, all tests have passed and the implementation is ready for review. Do not create PRs for work in progress.
- When writing the PR body, include a concise summary of the goal, work done, and clear instructions for reviewers on what to focus on in the review. Also include instructions on how to experience the any new/changed user experiences.
- Do not escape content in the PR or work-item description; use markdown formatting as needed for clarity and readability.
- After implementation, use the cleanup skill to tidy up branches and local state, but only after the PR is merged to avoid disrupting the review process.

## Handling Assets

- If the implementation requires the creation of assets such as graphics or audio files, create these assets in an appropriate subfolder of the `assets` directory (e.g., `assets/images/`, `assets/audio/`) and use a name that has the prefix "placeholder\_" followed by a descriptive name (e.g., `placeholder_player_explosion_spritesheet.png` or `placeholder_player_jump.wav`).
  - always reference new assets in the work item comments and PR description. Ensure that any generated assets are included in the commit and pushed to the repository.
  - when creating assets, ensure they are optimized for size and performance, and follow any project guidelines for asset creation and management.
  - you can discover assets on the web as part of your implementation, but ensure that you have the right to use and distribute any assets you include in the project. Always provide proper attribution if required by the asset's license.
  - any
- If the implementation requires changes to documentation, update the relevant markdown files in the `docs` directory and reference these changes in the work item comments and PR description.
  - ensure that documentation changes are clear, concise, and accurately reflect the implementation changes. Include examples or screenshots if they help clarify the documentation.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

0. Safety gate: handle dirty working tree

- Inspect `git status --porcelain=v1 -b`.
- If uncommitted changes are limited to `.worklog/`, carry them into the new working branch and commit there.
- If other uncommitted changes exist, pause and present explicit choices: carry them into the work item branch, commit first, stash (and optionally pop later), revert/discard (explicit confirmation), or abort.

1. Understand the work item

- Claim by running `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json` (omit `--assignee` if not applicable).
- Fetch the work item JSON if not already present: `wl show <work-item-id> --json` and `wl show <work-item-id> --json`.
- Restate acceptance criteria and constraints from the work item JSON.
- Surface blockers, dependencies and missing requirements.
- Inspect linked PRDs, plans or docs referenced in the work item.
- Confirm expected tests or validation steps.

  1.1) Definition gate (must pass before implementation)

- Verify:
  - Clear scope (in/out-of-scope).
  - Concrete, testable acceptance criteria.
  - Constraints and compatibility expectations.
  - Unknowns captured as explicit questions.
- If the work item is not well-defined, run the intake interview to update the existing work item (see `command/intake.md`) and update the work item `description` or `acceptance` fields with the intake output.
- If the work item is too large to implement in one pass, run plan interview (see `command/plan.md`) to break it into smaller work items, create those work items, link them as blockers/dependencies, and pick the highest-priority work item to implement next.
- If you ran the intake interview, update the current work item with the new definition and inform the user of your actions and ask if you should restart the implementation review.
- If you ran the plan interview, convert this work item to an epic and inform the user that implementation should move to the first child work item created.

2. Create a working branch

- inspect the current branch name via `git rev-parse --abbrev-ref HEAD`.
- If the current branch was created for a work item that is an ancestor of <work-item-id>, continue on that branch (that is if the name has an ancestor work item id).
- Otherwise create a new branch named `feature/<work-item-id>-<short>` or `bug/<work-item-id>-<short>` (include the work item id).
- Never commit directly to `main`.

3. Implement

- If the work item has any open or in_progress blockers or dependencies:
  - Select te most appropriate work item to work on next (blocker > dependency; most critical first).
  - Claim the work item by running `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`
  - Recursively implement that work item as described in this procedure.
  - When a work item is completed commit the work and update the stage: `wl update <work-item-id> --stage in_review --json`
- Write tests and code to ensure all acceptance criteria defined in or related to the current work item are met:
  - Make minimal, focused changes that satisfy acceptance criteria.
  - Follow a test-driven development approach where applicable.
  - Ensure code follows project style and conventions.
  - Add comments to the work item describing any significant design decisions, code edits or tradeoffs.
  - If additional work is discovered, create linked work items: `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all acceptance criteria for the primary work item and all blockers and dependents are met:
  - Run the entire test suite.
    - Report the reults
    - Fix any failing tests before continuing.
    - If the test run discovers failing tests that appear to be outside the scope or ownership of the current work item (e.g., failures in files not modified by this branch), invoke the triage helper to search or create a critical test-failure issue:
      - Example: `python3 skill/triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"..."}'`
      - If `check_or_create` returns that it created a NEW critical issue for this run, record the created issue id in the agent's local run-state and DO NOT proceed to create a PR for the current work item while that issue remains open, unless the PR will explicitly reference and close that issue.
      - If an existing incomplete critical issue was matched, append any new evidence as a comment (the helper will do this) and proceed: pre-existing critical issues do not block PR creation for unrelated agent work.
  - Update or create relevant documentation.
  - Summarize changes made in the work item description or comments.
  - Do not proceed to the next step until the user confirms it is OK to do so.

4. Automated self-review

- Audit the work item to confirm all acceptance criteria are met: `audit <work-item-id> using the audit skill`.
  - If the audit reveals any unmet acceptance criteria, inform the user of the findings and return to step 3 to address them.
- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, produce a short note and limit edits to small, goal-aligned changes. If intent changes are discovered, create an Open Question and stop automated edits.
- Run the entire test suite.
  - Fix any failing tests before continuing.

5. Commit, Push and create PR

- Ensure all work has been committed
- Push the branch to `origin`.
- Create a Pull Request (PR) against the repository's default branch.
  - Use a title in the form of a summary of the goal and a body that contains
    - a summary of the goal
    - a summary of the work done
    - instructions on how to test manually (where relevant)
    - instructions on what to focus on in the review (e.g., "focus on the new authentication flow and any potential edge cases").
    - reference the work item id(s) and link to any relevant documentation or PRDs.
    - Ensure that the desciption covers all commits and work items involved in the implementation
  - Do not escape the PR body; use markdown formatting as needed.
- Link the PR to the work-item in a work-item comment to the work item as follows `wl comment <work-item-id> --body "PR created: <URL>\nReady for review and merge." --author "<AGENT>" --json`.
- Mark the work item to completed/in-review with `wl update <work-item-id> --status completed --stage in_review --json`

Pre-PR blocking check
---------------------
- Before creating a PR, inspect the agent-run local state for any NEW critical test-failure issues created during this run. If any exist and remain open, the agent MUST not create a PR for the current work item unless the PR explicitly references and closes the created critical issue(s). Pre-existing critical issues (created prior to this agent run) are informational and do not block PR creation.
