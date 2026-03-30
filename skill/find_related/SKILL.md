---
name: find_related
description: Discover related work for a Worklog work item and generate a concise, auditable "Related work" report that can be appended to the work item description.
---

## Purpose

Provide a deterministic, agent-friendly way to discover existing or prior work related to a
work item. The skill searches Worklog (open + closed), inspects repository files and docs with
conservative heuristics, and can optionally generate an LLM-backed "Related work (automated report)"
that is inserted into the work item description under a clearly-marked section.

## When to use

- When a work item needs evidence of related or precedent work before planning or implementation.
- When the intake process wants to augment context with an automated report without replacing human authored intake drafts.
- When a user or agent asks questions like "has this been done before?", "what's related?", or "is there existing context I should be aware of?" in relation to a work item.

## Inputs

- work-item id (required)

## Outputs

- JSON summary printed to stdout, keys:
  - found (boolean)
  - addedIds (array of work item ids appended)
  - reportInserted (boolean)
  - updatedDescription (string)

## Decision logic

1. Fetch work item: `wl show <id> --json`.
2. If the description already contains related markers (e.g., `related-to:`) extract these as existing related items and carry them into the following steps.
3. Derive conservative keywords from the title/description/comments and run `wl search <keyword> --json` to collect candidates related issus.
4. For each candidate, fetch details with `wl show <id> --json` and review title, description, acceptance criteria, and comments to determine if it is truly related. Only include items that have clear relevance to the work item goals or context.
5. Use the `wl deps list <id> --json` command to identify any dependencies and include these in the list of repo matches to review for relevance.
6. Search repository documentation and code files for matching keywords; include those as repo matches.

- ignore data directories such as `node_modules`, `.git` and most "." named folders.

7. Produce a short informational report describing related work in the repository, using the previously discovered items as seeds. The report MUST:

- be clearly labeled and inserted under the heading "Related work (automated report)".
- include links to any related work items or docs discovered, along with their titles or file paths.
- Describe the relevance of each related item or doc in 1–2 sentences. This is the key value-add of the report, so it should not just be a list of links but should provide insight into why each item is related.

8. Update the item description by appending the generated report. Note, if the existing description already contains a related items report or markers, the new report can replace this content, but ONLY this content. Use `wl update` to perform the update.
9. Return the JSON summary.

## Hard requirements

- Default behaviour must be conservative: prefer false negatives over false positives when writing the report.
- Review each candidate item to ensure it is truly related before including it in the report. Do not include items that are only tangentially related or have low relevance.

End.
