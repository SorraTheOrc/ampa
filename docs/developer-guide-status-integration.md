# Developer Guide: Integrating the `input_needed` status

This guide explains the technical considerations for integrating the `input_needed` status into AMPA and Worklog integrations.

Background
- `input_needed` is an additive status used to mark work items that require more information from the requester. It should be treated as orthogonal to existing stages (idea, intake_complete, plan_complete, in_progress, in_review, done).

Requirements
- The status must be accepted by `wl create` and `wl update` commands.
- Automation (scheduler, intake runners) must not progress items while `input_needed` is set.

Implementation notes
- Schema/Enums: If Worklog stores statuses as an enum, add `input_needed` to the allowed values and update any validation logic.
- CLI: Ensure `wl update <id> --status input_needed` works and that `wl list --status input_needed` returns items.
- Automation: Intake runners should set `input_needed` when the intake process cannot proceed. They should also add explicit open questions to the work item (comments or a designated field).
- Idempotency: Setting and clearing the status should be idempotent. Ensure automated processes check current status before making updates.

Integration points in AMPA
- Intake dispatcher (engine/dispatch.py): When an opencode intake run determines missing information, call Worklog API to set `status=input_needed` and add a comment containing the follow-up questions.
- Scheduler (scheduler.py / scheduler_helpers.py): Filters should exclude `input_needed` items when selecting candidates for automatic progression or assignment.
- UI/TUI: Update any status lists or help text to include `input_needed` and explain its meaning.

Testing
- Unit tests: Add tests for status validation, ensuring `input_needed` is accepted wherever statuses are validated.
- Integration tests: Simulate an intake run that detects missing info and assert the work item ends up with `status=input_needed` and contains the expected questions in comments.
- CLI tests: Ensure `wl list --status input_needed` and `wl update <id> --status input_needed` behave as expected.

Example pseudo-code (setting status during intake):

```
# intake_result contains a list of missing fields or questions
if intake_result.missing:
    wl_client.update(item_id, status="input_needed")
    wl_client.comment(item_id, "Open question: ...")
    return

# otherwise, mark intake complete
wl_client.update(item_id, stage="intake_complete")
```

Migration and backward compatibility
- Adding a new status is additive. Existing items should remain unchanged.
- If downstream systems rely on a fixed set of statuses, notify maintainers and update integrations accordingly.

Documentation
- Update README.md and help text to include `input_needed` and refer operators to docs/operator-guide-input-needed.md.

See also: docs/operator-guide-input-needed.md
