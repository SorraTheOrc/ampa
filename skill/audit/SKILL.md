---
name: audit
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. When no work item id is provided, run `wl` CLI tool to summarize recent work and current work in progress.

## When To Use

- User asks general project status (e.g., "What is the current status?", "Status of the project?", "status", "audit the project", "audit").
- User asks about a specific work item id (e.g., "What is the status of wl-123?", "status wl-123", "audit wl-123").

## Best Practices

- Output should be formatted as markdown for readability.
- When summarizing work items, focus on actionable information: current status, blockers, dependencies.
- When a work item id is provided, ensure to include all relevant details and related work items including dependencies (`wl dep list <work-item-id> --json`) and subtasks (`wl show <work-item-id> --json`) in the summary.
- Always conclude with a clear summary of the status.
  - For individual work items, the last line of the summary should be a clear statement about whether the work item can be closed or not.
- Do not recommend next steps or actions; this skill focuses on reporting only.

## Steps

1. Detect whether the user provided a work item id in the request.

2. If no work item id is provided complete this step, otherwise skip to step 3:

- Run `wl list --json` to fetch work items in JSON format to get more information, but do not display it.
- Present a one line summary of the overall project status based on the JSON data. Including:
  - Total number of critical and high priority work items.
  - Total number of open work items.
  - Total number of in_progress work items.
  - Total number of blocked work items.
- Present a summary of actively in_progress work items (`wl in-progress --json`). For each in_progress item, include: title, id, assignee, priority, and a one line summary of the description.

Skip to step 6.

3. If a work item id is provided:

- Run `wl show <work-item-id> --children --json` to fetch work item details (with all comments and children).
- Extract the acceptance criteria from the description (they are usually in a markdown section starting with `## Acceptance Criteria` or `### Acceptance Criteria` and formatted as a numbered or bulleted list).
  - If no acceptance criteria section is found, note: "No acceptance criteria defined."
- Walk through all dependencies (`wl dep list <work-item-id> --json`) and list each dependent work-item's status, title (using strike through if the item has a "completed" status), id, and stage.

4. **Deep code review of acceptance criteria (parent work item):**

For each acceptance criterion found in step 3, perform a thorough code review:
- Read the actual implementation files referenced in the work item description, comments, or discoverable from the codebase.
- Assess correctness: does the code implement what the criterion requires?
- Assess completeness: are edge cases handled? Are there missing branches or error paths?
- Check for test coverage: are there tests that validate this criterion?
- Assign a verdict to each criterion:
  - `met` — the criterion is fully satisfied by the implementation
  - `unmet` — the criterion is not satisfied or the implementation is missing
  - `partial` — the criterion is partially satisfied but incomplete
- For each verdict, provide a one-line evidence note referencing the relevant file and line number (e.g., `src/handler.ts:42 — rate limiter middleware correctly intercepts requests`).
- Do NOT rely solely on work item descriptions and comments. You MUST read the actual code to verify.

5. **Deep code review of children's acceptance criteria:**

For each direct child work item (do NOT recurse into grandchildren):
- Run `wl show <child-id> --json` to fetch the child's details.
- Extract the child's acceptance criteria from its description.
  - If no acceptance criteria section is found, note: "No acceptance criteria defined."
- Perform the same deep code review as described in step 4 for each of the child's acceptance criteria.
- Assign per-criterion verdicts (`met`/`unmet`/`partial`) with file:line evidence.

6. **Produce the structured audit report:**

Wrap the final report output in delimiter markers. The report MUST follow this exact structure:

```
Ready to close: Yes/No

## Summary

<concise 2-4 sentence summary of overall status, key findings, and whether the item can be closed>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial | <file_path:line_number — one-line note> |
| 2 | ... | ... | ... |

<If no acceptance criteria were found, write: "No acceptance criteria defined.">

## Children Status

### <child-title> (<child-id>) — <status>/<stage>

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial | <file_path:line_number — one-line note> |

<Repeat for each direct child. If a child has no acceptance criteria, write: "No acceptance criteria defined.">
<If there are no children, write: "No children.">
```

CRITICAL rules for the structured report:
- The first line must be `Ready to close: Yes` or `Ready to close: No` based on whether all acceptance criteria (parent and children) are met.
- Keep the report concise despite the deep analysis. Each evidence note should be ONE line.
- For project-level audits (no work item id), omit the `## Acceptance Criteria Status` and `## Children Status` sections. Include only `## Summary` and `## Recommendation`.
- Review only direct children, never grandchildren. If there are many children (>10), note in the report that only the first 10 were reviewed and the rest were omitted for brevity.

7. **Record the audit using the CLI structured write path:**

   - Run: `wl update <work-item-id> --audit-text "<complete-report-content>" --json`
    - This stores the audit as structured metadata, making it machine-readable and queryable via `wl show --json`.

## Notes

- Keep the output concise and actionable for quick human consumption.
- Handle errors gracefully: if `wl` or any other command is not available or return invalid JSON, present a helpful error and possible remediation steps.
- The depth of code review is critical: read implementation files, check function signatures, verify test coverage, and assess edge cases. Do not just check that files exist.
