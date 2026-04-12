# Operator Guide: Handling work items with status `input_needed`

This document explains how operators should handle work items marked with the `input_needed` status.

When to use `input_needed`:
- The status indicates the intake process or an agent has determined a work item lacks necessary information from the requester and cannot progress.
- It is set when an automated intake or human reviewer needs additional details (requirements, repro steps, attachments, scope clarification).

Key principles:
- `input_needed` is a status, not a stage. A work item may be in any stage while having this status.
- Treat `input_needed` as a blocker for automatic progression: intake runners and automated assignment should not advance items while this status is set.

Operator workflow:
1. Triage list: Use `wl list --status input_needed` to surface items awaiting requester input.
2. Review questions: Open the work item and read the open questions section (agents or humans should add explicit questions in the comments or work item body).
3. Contact requester: Reach out to the requester via the channel used to create the request (email, issue comment, or ticketing system) and ask for the missing information.
4. Update the item: When the requester supplies the information, add it to the work item (comments or attachments) and remove `input_needed` by running:

```
wl update <id> --status open
```

Or set an appropriate status consistent with your workflow (for example `in-progress` or `open`).

5. Continue intake: The intake automation or operator should then rerun the intake process or manually advance the stage to `intake_complete` when appropriate.

Best practices:
- Keep questions specific and actionable.
- Prefer editing the existing work item with new information rather than creating new items.
- If a requester is unresponsive for a long time, add a comment summarising attempts to contact and set a reminder or follow-up policy.

Troubleshooting:
- If `wl list --status input_needed` returns no results but automation reported input-needed during intake, verify the intake logs and agent comments for the item id.
- If automation repeatedly sets `input_needed` for the same item, inspect the intake transcript to determine if the automation expects a different form or format of information. Consider escalating to developers if the intake rules are ambiguous.

Related commands:
- `wl list --status input_needed`
- `wl show <id> --children --json`
- `wl update <id> --status open`

See also: docs/developer-guide-status-integration.md
