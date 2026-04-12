# Operator Guide: Handling "input_needed" Status

This document explains how operators should handle work items marked with the `input_needed` status.

When an automated intake or an operator identifies that a work item lacks the information required to proceed, the work item should be set to the `input_needed` status and questions should be recorded on the work item.

Operator responsibilities

- Review open work items with `status: input_needed` regularly. Use `wl list --status input_needed` to find them.
- Read the open questions recorded in the work item comments and, if possible, contact the requester to obtain the missing information.
- When the requester provides the required information, update the work item by:
  - Adding the information to the work item description or comments
  - Clearing or resolving the open questions
  - Setting the status back to `open` or another appropriate status (`in-progress`, etc.) using `wl update <id> --status open`

Notes for intake automation

- Automated intake processes (for example, the scheduled `intake-runner`) may set the `input_needed` status when the intake interview yields unresolved questions.
- The automation should create explicit open questions as comments so operators and requesters can see what is missing.

Example workflow

1. Intake process runs and discovers missing fields A and B.
2. Automation records questions in the work item comments and sets `--status input_needed`.
3. Operator reviews `wl list --status input_needed`, opens the work item and triages.
4. Operator or requester answers the questions in comments and updates the description.
5. Operator sets `wl update <id> --status open` to return the item to the normal workflow.

Troubleshooting

- If many items are stuck in `input_needed`, review the intake script for overly strict validation.
- If requesters do not respond, consider adding a follow-up comment tagging the requester and setting a reminder or creating a follow-up work item.

Related commands

- `wl list --status input_needed`
- `wl show <id>`
- `wl update <id> --status open`
