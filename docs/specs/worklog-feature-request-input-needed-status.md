# Feature Request: Add 'input_needed' Status to Worklog System

**Requester**: AMPA Project Team  
**Date**: April 11, 2026  
**Priority**: High  
**Related Work Items**: AM-0MNU8KC46006G5F4, AM-0MNU7MXP40060TW7  

---

## Summary

Request the addition of a new status value `input_needed` to the Worklog system's valid status enumeration. This status is required to support automated intake processes that identify when a work item requires additional information from the requester before it can proceed.

---

## Motivation

The AMPA (Automated Multi-Project Assistant) system implements an automated intake process for work items. When the intake process identifies missing or insufficient information in a work item, it needs to:

1. Flag the work item as requiring input
2. Record specific questions that need answers
3. Notify the requester that action is required
4. Allow the work item to remain in its current stage while awaiting input

Currently, the Worklog system does not have a status value that represents this state. The existing statuses (`open`, `in-progress`, `completed`, `blocked`, `deleted`) do not adequately capture the semantic meaning of "awaiting requester input."

---

## Proposed Change

### New Status Value

Add `input_needed` as a valid status value in the Worklog system.

**Semantics**: The work item requires additional information, clarification, or action from the requester or stakeholders before work can proceed. This is distinct from `blocked` (which implies an external blocker) and `open` (which implies ready to start).

### Updated Status Enumeration

Current valid statuses:
- `open` - Work item is ready to be worked on
- `in-progress` - Work is actively being done
- `completed` - Work has been finished
- `blocked` - Work is blocked by external factors
- `deleted` - Work item has been removed

**Proposed updated valid statuses**:
- `open` - Work item is ready to be worked on
- `in-progress` - Work is actively being done
- `completed` - Work has been finished
- `blocked` - Work is blocked by external factors
- **`input_needed`** - **Work item requires additional information from requester**
- `deleted` - Work item has been removed

---

## Usage Examples

### Example 1: Automated Intake Process

```bash
# 1. Work item is created with status "open" and stage "idea"
wl create --title "New feature request" --description "..."
# Created: WL-12345

# 2. Automated intake system identifies missing requirements
# 3. System updates status to input_needed and adds questions
wl update WL-12345 --status input_needed
wl comment add WL-12345 --comment "Open questions:\n- What is the expected timeline?\n- Which users are affected?"

# 4. Requester provides answers
wl comment add WL-12345 --comment "Answers:\n- Timeline: Q3 2026\n- Users: All authenticated users"
wl update WL-12345 --status open --stage intake_complete
```

### Example 2: Manual Status Update

```bash
# Developer realizes they need more information
wl update WL-67890 --status input_needed

# Later, when information is received
wl update WL-67890 --status in-progress
```

### Example 3: Filtering Work Items Awaiting Input

```bash
# List all work items awaiting input
wl list --status input_needed

# List high priority items awaiting input
wl list --status input_needed --priority high

# Count items awaiting input
wl list --status input_needed --json | jq '. | length'
```

---

## Filtering Support

The new status MUST be fully supported by the `wl list` command's filtering capabilities:

- `wl list --status input_needed` - Returns all work items with input_needed status
- `wl list --status input_needed --stage idea` - Returns input_needed items in idea stage
- `wl list --status open,in-progress,input_needed` - Combined status filter
- `wl next --status input_needed` - Should NOT recommend items with this status (as they cannot proceed without input)

### Expected Output Format

```json
[
  {
    "id": "WL-12345",
    "title": "New feature request",
    "status": "input_needed",
    "stage": "idea",
    "priority": "high",
    "assignee": "@developer"
  }
]
```

If no work items match the filter, the command should return an empty list (no error).

---

## Compatibility Requirements

### Backward Compatibility

- **MUST** be purely additive - no changes to existing status values
- **MUST NOT** affect existing work items or their status values
- **MUST NOT** change behavior of existing status transitions
- Existing integrations should continue to work without modification

### Stage Compatibility

The `input_needed` status MUST be compatible with ALL existing stages:

- `idea` - Item needs clarification before intake can complete
- `intake_complete` - Item passed intake but now needs additional info
- `plan_complete` - Planning revealed new unknowns requiring input
- `in_progress` - Implementation questions arose needing clarification
- `in_review` - Review raised questions for the original requester
- `done` - (Edge case: reopened item needs clarification)

### Status Transition Rules

Valid transitions TO `input_needed`:
- `open` → `input_needed`
- `in-progress` → `input_needed`
- `blocked` → `input_needed`

Valid transitions FROM `input_needed`:
- `input_needed` → `open` (information received, ready to proceed)
- `input_needed` → `in-progress` (information received, resuming work)
- `input_needed` → `blocked` (information request itself is blocked)
- `input_needed` → `completed` (edge case: item resolved without input)
- `input_needed` → `deleted` (item abandoned)

### CLI Compatibility

All existing CLI commands MUST support the new status:
- `wl create --status input_needed`
- `wl update <id> --status input_needed`
- `wl list --status input_needed`
- Status validation must accept the new value

### TUI Compatibility

The status MUST appear correctly in:
- Work item detail views
- List views (with appropriate color/icon if applicable)
- Status selection dropdowns/completions

---

## Technical Requirements

### 1. Schema Changes

**Location**: Worklog database schema and type definitions

**Changes Required**:
- Update status enumeration/type definition to include `input_needed`
- Ensure validation logic accepts the new status value
- Update any hardcoded status lists (if applicable)

**Example** (pseudo-code):
```python
# Before
VALID_STATUSES = ["open", "in-progress", "completed", "blocked", "deleted"]

# After
VALID_STATUSES = ["open", "in-progress", "completed", "blocked", "input_needed", "deleted"]
```

### 2. Validation Updates

**Location**: Status validation logic

**Changes Required**:
- Add `input_needed` to status validator
- Update any regex patterns that match status values
- Ensure case-sensitive matching (lowercase with underscore)

### 3. CLI Support

**Location**: CLI argument parsing and help text

**Changes Required**:
- Update `--status` flag help text to include new option
- Update shell completions (if applicable)
- Ensure status values are properly normalized/validated

**Example help text update**:
```
--status string   Filter by status (open, in-progress, completed, blocked, input_needed, deleted)
```

### 4. TUI Updates

**Location**: TUI rendering and status display

**Changes Required**:
- Add display representation for `input_needed` status
- Update status color/icon mapping (if applicable)
- Ensure the status appears in selection interfaces

**Suggested visual treatment**:
- Color: Yellow/Amber (indicating "attention needed")
- Icon: Question mark or similar indicator (if using icons)

### 5. JSONL/Git Sync

**Location**: JSONL export/import and Git sync

**Changes Required**:
- Ensure the status value serializes/deserializes correctly
- No special migration needed (new field value only)

### 6. Documentation Updates

**Location**: Worklog documentation

**Changes Required**:
- Update status reference documentation
- Add `input_needed` to CLI help examples
- Update any status-related tutorials or guides

---

## Test Requirements

### Unit Tests

1. **Status Validation**
   - Test that `input_needed` is accepted as valid status
   - Test that invalid statuses are still rejected
   - Test case sensitivity (reject `Input_Needed`, `INPUT_NEEDED`, etc.)

2. **Status Transitions**
   - Test valid transitions TO `input_needed` from all statuses
   - Test valid transitions FROM `input_needed` to all statuses
   - Test invalid transitions (if any are restricted)

3. **CLI Commands**
   - Test `wl create --status input_needed`
   - Test `wl update <id> --status input_needed`
   - Test `wl list --status input_needed` returns correct items
   - Test `wl list --status input_needed --json` output format

### Integration Tests

1. **End-to-End Workflow**
   - Create work item with `input_needed` status
   - Update work item to/from `input_needed` status
   - Query work items by `input_needed` status
   - Verify persistence across sync operations

2. **TUI Integration**
   - Verify `input_needed` appears in TUI list views
   - Verify `input_needed` displays with correct formatting
   - Test status selection in TUI interfaces

3. **Filtering Integration**
   - Test combined filters: `--status input_needed --stage idea`
   - Test multiple status filter: `--status open,input_needed`
   - Verify sort order and pagination work correctly

### Regression Tests

1. **Backward Compatibility**
   - Verify existing work items with other statuses are unaffected
   - Verify existing scripts using status values continue to work
   - Verify no changes to existing status behaviors

2. **Edge Cases**
   - Test empty list when no items match `input_needed` filter
   - Test special characters in work item titles with input_needed status
   - Test concurrent updates to input_needed status

---

## Success Criteria

The implementation is considered successful when:

1. ✅ `input_needed` is a valid status value accepted by all Worklog commands
2. ✅ `wl list --status input_needed` correctly filters and returns matching work items
3. ✅ The status can be set on work items in any stage
4. ✅ The status persists correctly through sync and Git operations
5. ✅ All existing functionality remains backward compatible
6. ✅ Full test suite passes with the new status
7. ✅ Documentation is updated to reflect the new status option
8. ✅ TUI displays the status correctly with appropriate visual treatment

---

## Implementation Checklist for Worklog Team

- [ ] Update status type/enum definition
- [ ] Update status validation logic
- [ ] Update CLI argument parsing and help text
- [ ] Update TUI status display (color/icon)
- [ ] Add unit tests for status validation
- [ ] Add integration tests for CLI commands
- [ ] Add regression tests for backward compatibility
- [ ] Update documentation
- [ ] Run full test suite
- [ ] Notify AMPA team of completion

---

## Appendix: Related Context

### Parent Work Item
**AM-0MNU8KC46006G5F4**: "Add 'input_needed' status to Worklog system"  
Full specification of the feature from the AMPA project perspective.

### Related Work Item
**AM-0MNU7MXP40060TW7**: "Automated intake scheduled task"  
The automated intake process that will use the `input_needed` status.

### Use Case Details

The automated intake system will:
1. Query for work items in "idea" stage
2. Spawn an opencode session to conduct an intake interview
3. If the intake agent has questions, it will:
   - Add the questions as comments to the work item
   - Set the status to `input_needed`
4. When the requester responds with answers:
   - The status is changed back to `open`
   - The stage is set to `intake_complete`

This workflow requires the `input_needed` status to properly track items that are stalled waiting for requester input.

---

## Contact

For questions or clarifications about this feature request, please contact the AMPA project team or reference the related work items above.

---

**Document Version**: 1.0  
**Last Updated**: April 11, 2026  
**Status**: Ready for Worklog Team Review
