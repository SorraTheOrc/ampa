Operator guide: Intake runner

Overview

The intake-runner is a non-interactive scheduler command that selects a single work item in stage `idea`, dispatches an automated `/intake <id> do not ask questions` opencode session, records outcomes and basic metrics, and logs failures for operator follow-up.

When to run

- The scheduler runs the intake-runner automatically according to the configured command frequency.
- Operators may run it manually via the scheduler CLI when investigating backlog items.

How it works (high level)

1. Query: `wl next --stage idea --json` via the IntakeCandidateSelector to obtain candidates.
2. Select: deterministic single-item selection by sortIndex (descending), updated timestamp tie-break and id for determinism.
3. Dispatch: an intake session is spawned using the IntakeDispatcher which builds the canonical intake command and delegates to the configured dispatcher (OpenCodeRunDispatcher or ContainerDispatcher).
4. Observe: previously-recorded dispatches are inspected and `wl show <id> --children --json` is used to detect `intake_complete` or `input_needed` outcomes; outcomes are stored in SchedulerStore.intake_metrics.
5. Retry/backoff: per-item retry state is maintained in SchedulerStore under `intake_retries` with exponential backoff and a configurable max_retries.

Operator-visible artifacts

- Scheduler store: .worklog/ampa/scheduler_store.json persists dispatch records (append-only `dispatches`), per-command `state`, `intake_metrics`, and retry/backoff state.
- Worklog comments: automated comments are added to the work item when an intake is dispatched or when dispatch attempts fail.
- Audit files: audit reports and operator notifications are saved to .worklog/audit when the audit poller is used.

Configuration and environment

- Command registration: the intake-runner is exposed as a scheduler command (command_id `intake-selector` / command_type `intake`). See the scheduler descriptor in docs/workflow/workflow.json.
- Environment variables:
  - AMPA_INTAKE_TIMEOUT: intake session timeout in seconds (default 3600).
  - AMPA_INTAKE_COMPLETION_TIMEOUT: seconds to wait before treating a dispatch as timed out (default 4*3600).
  - AMPA_CONTAINER_DISPATCH_TIMEOUT: container dispatch timeout used by ContainerDispatcher.
- Per-command metadata keys (in command spec):
  - max_retries (int): maximum dispatch attempts before marking permanent failure.
  - backoff_base_minutes (float): base minutes used for exponential backoff.

Logs and observability

- Dispatch records: SchedulerStore.append_dispatch generates a stable dispatch id and records `ts` (ISO8601) and `session` fields for traceability.
- DispatchResult includes a `timestamp` and `pid` (or `container_id`) when available; these are copied into the persistent dispatch record.
- Intake metrics: `intake_metrics` in scheduler state contains `started_at`, `completed_at`, `outcome`, and `duration_seconds` per work item.
- Notifications: operator-facing notifications are posted via the configured notifier (Discord) and comments are added to work items.

Testing and validation

- Unit tests: tests/test_intake_selector.py and tests/test_intake_dispatcher.py cover selection and dispatch logic.
- Integration tests: tests/test_intake_integration.py and scheduler integration tests exercise end-to-end behaviour including selection persistence and retry paths.

Operator actions

- To run manually: use the scheduler CLI or invoke the intake-selector command via the scheduler interface.
- To inspect recent dispatches: open .worklog/ampa/scheduler_store.json and examine the `dispatches` array or use the scheduler debug tools.
- To investigate an intake outcome: check the work item comments, `intake_metrics` in the scheduler store, and the `.worklog/audit` directory for audit reports.

Troubleshooting

- If no candidates are processed: ensure the scheduler is not global-rate-limited (SchedulerStore.last_global_start and scheduler config `global_min_interval_seconds`) and that `wl next --stage idea` returns candidates.
- If dispatch fails repeatedly: check `intake_retries` in the scheduler state and relevant notifications; permanent failures will be recorded and operators notified.

Contact

- For operational questions, contact the AMPA maintainers (see project CODEOWNERS) or open an operator issue in the project's backlog.
