# Developer Guide: Integrating `input_needed` Status

This document describes how developers should integrate the new `input_needed` status into code that interacts with Worklog.

Overview

`input_needed` is a status (not a stage). A work item may be in any stage while having the `input_needed` status set. The status indicates the item requires additional information from the requester before work can continue.

API and CLI

- The Worklog CLI and API will accept `input_needed` as a valid status value. Use `wl update <id> --status input_needed` to mark an item.

Validation and transitions

- Treat `input_needed` as orthogonal to stage transitions. Do not prevent stage changes solely because an item has `input_needed` set; instead, make business logic decisions based on both fields when appropriate.
- When consuming status values, ensure code handles unknown/future status values gracefully (e.g., by falling back to default behaviours or treating unknown statuses as `open`).

Example usage

1. Intake runner detects missing information:

```text
wl update AM-XXXXX --status input_needed
wl comment add AM-XXXXX --comment "Missing field: acceptance criteria; please provide"
```

2. Operator or requester fills in the details and clears the status:

```text
wl update AM-XXXXX --status open
```

Testing

- Add unit tests to ensure status validation accepts `input_needed` where statuses are validated.
- Add integration tests for automation that may set this status during intake.

Documentation

- Update any enum/type definitions that list valid statuses.
- Add `input_needed` to lists that describe Worklog statuses in README and CLI help text.
