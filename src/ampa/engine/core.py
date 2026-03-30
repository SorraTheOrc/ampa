"""Engine core — orchestrates the 4-step command execution lifecycle.

Coordinates candidate selection, invariant evaluation, state transitions,
context assembly, and dispatch into a cohesive engine that processes
work items according to the workflow descriptor.

Supports two invocation modes:

- **Mode A — Initial dispatch** (``process_delegation``):
  Scheduler calls the engine to select a candidate, evaluate pre-invariants,
  apply the dispatch state transition, and spawn an independent agent session.

- **Mode B — Agent callback** (``process_transition``):
  A delegated agent requests a state transition; the engine evaluates
  post-invariants and applies or refuses the transition.

Usage::

    from ampa.engine.core import Engine, EngineConfig

    engine = Engine(
        descriptor=descriptor,
        dispatcher=dispatcher,
        candidate_selector=selector,
        invariant_evaluator=evaluator,
        work_item_fetcher=fetcher,
        config=EngineConfig(descriptor_path="docs/workflow/workflow.yaml"),
    )
    result = engine.process_delegation()
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from ampa.engine.candidates import CandidateResult, CandidateSelector
from ampa.engine.descriptor import StateTuple, WorkflowDescriptor
from ampa.engine.dispatch import DispatchResult, Dispatcher
from ampa.engine.invariants import InvariantEvaluator, InvariantResult

LOG = logging.getLogger("ampa.engine.core")


# ---------------------------------------------------------------------------
# Protocols for external dependencies (mockable)
# ---------------------------------------------------------------------------


class WorkItemUpdater(Protocol):
    """Protocol for applying state transitions to work items.

    Wraps ``wl update <id> --status <s> --stage <g>`` calls.
    """

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        """Apply a state update.  Returns ``True`` on success."""
        ...


class WorkItemCommentWriter(Protocol):
    """Protocol for writing comments to work items."""

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        """Write a comment. Returns ``True`` on success."""
        ...


class DispatchRecorder(Protocol):
    """Protocol for recording dispatch events in the audit trail."""

    def record_dispatch(self, record: dict[str, Any]) -> str | None:
        """Append a dispatch record. Returns the record ID or ``None``."""
        ...


class NotificationSender(Protocol):
    """Protocol for sending notifications (e.g. Discord bot messages)."""

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        """Send a notification. Returns ``True`` on success."""
        ...


class WorkItemFetcher(Protocol):
    """Protocol for fetching work item data from ``wl show``.

    Returns the raw ``wl show {id} --children --json`` output as a dict,
    or ``None`` on failure.
    """

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        """Fetch work item data.  Returns the JSON dict or ``None``."""
        ...


# ---------------------------------------------------------------------------
# Null implementations (no-op defaults)
# ---------------------------------------------------------------------------


class NullUpdater:
    """No-op updater (always succeeds)."""

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        return True


class NullCommentWriter:
    """No-op comment writer."""

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        return True


class NullDispatchRecorder:
    """No-op dispatch recorder."""

    def record_dispatch(self, record: dict[str, Any]) -> str | None:
        return None


class NullNotificationSender:
    """No-op notification sender."""

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        return True


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class EngineStatus:
    """Engine result status constants."""

    SUCCESS = "success"
    NO_CANDIDATES = "no_candidates"
    REJECTED = "rejected"  # from-state mismatch
    INVARIANT_FAILED = "invariant_failed"
    DISPATCH_FAILED = "dispatch_failed"
    UPDATE_FAILED = "update_failed"  # wl update failed
    SKIPPED = "skipped"  # audit_only or hold mode
    ERROR = "error"  # unexpected error


@dataclass(frozen=True)
class EngineResult:
    """Result of an engine invocation.

    Provides a complete audit trail of what happened and why.
    """

    status: str
    reason: str = ""
    work_item_id: str | None = None
    command_name: str | None = None
    action: str | None = None
    dispatch_result: DispatchResult | None = None
    invariant_result: InvariantResult | None = None
    candidate_result: CandidateResult | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def success(self) -> bool:
        """Whether the engine completed its work successfully."""
        return self.status == EngineStatus.SUCCESS

    def summary(self) -> str:
        """Human-readable summary for logging and notifications."""
        parts = [f"Engine result: {self.status}"]
        if self.work_item_id:
            parts.append(f"Work item: {self.work_item_id}")
        if self.action:
            parts.append(f"Action: {self.action}")
        if self.reason:
            parts.append(f"Reason: {self.reason}")
        if self.dispatch_result and self.dispatch_result.success:
            parts.append(f"PID: {self.dispatch_result.pid}")
        if self.invariant_result and not self.invariant_result.passed:
            parts.append(
                f"Failed invariants: {self.invariant_result.failed_invariants}"
            )
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineConfig:
    """Engine configuration.

    Attributes:
        descriptor_path: Path to the workflow descriptor file.
        max_concurrency: Maximum concurrent delegations (default 1).
        fallback_mode: Override from scheduler fallback config (hold/auto-decline/auto-accept).
        audit_only: If True, skip delegation entirely.
    """

    descriptor_path: str = ""
    max_concurrency: int = 1
    fallback_mode: str | None = None
    audit_only: bool = False


# ---------------------------------------------------------------------------
# Engine core
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return current UTC time (extracted for testability)."""
    return datetime.now(timezone.utc)


class Engine:
    """AMPA Engine core orchestrator.

    Coordinates the 4-step command execution lifecycle as defined in
    ``docs/workflow/engine-prd.md``:

    1. Confirm from-state
    2. Evaluate pre-invariants
    3. Apply state transition
    4. Execute command logic (dispatch)

    Parameters
    ----------
    descriptor:
        Loaded workflow descriptor providing commands, invariants, states.
    dispatcher:
        Implementation for spawning agent sessions.
    candidate_selector:
        Implementation for selecting work item candidates.
    invariant_evaluator:
        Implementation for evaluating pre/post invariants.
    work_item_fetcher:
        Implementation for fetching work item data (``wl show``).
    updater:
        Implementation for applying ``wl update`` state transitions.
    comment_writer:
        Implementation for writing ``wl comment add`` comments.
    dispatch_recorder:
        Implementation for recording dispatch events.
    notifier:
        Implementation for sending notifications (Discord).
    config:
        Engine configuration.
    clock:
        Callable returning current UTC datetime (for testing).
    """

    def __init__(
        self,
        descriptor: WorkflowDescriptor,
        dispatcher: Dispatcher,
        candidate_selector: CandidateSelector,
        invariant_evaluator: InvariantEvaluator,
        work_item_fetcher: WorkItemFetcher,
        updater: WorkItemUpdater | None = None,
        comment_writer: WorkItemCommentWriter | None = None,
        dispatch_recorder: DispatchRecorder | None = None,
        notifier: NotificationSender | None = None,
        config: EngineConfig | None = None,
        clock: Any = None,
    ) -> None:
        self._descriptor = descriptor
        self._dispatcher = dispatcher
        self._selector = candidate_selector
        self._evaluator = invariant_evaluator
        self._fetcher = work_item_fetcher
        self._updater = updater or NullUpdater()
        self._comment_writer = comment_writer or NullCommentWriter()
        self._recorder = dispatch_recorder or NullDispatchRecorder()
        self._notifier = notifier or NullNotificationSender()
        self._config = config or EngineConfig()
        self._clock = clock or _utc_now

    # ------------------------------------------------------------------
    # Mode A: Initial dispatch (scheduler-driven)
    # ------------------------------------------------------------------

    def process_delegation(
        self,
        work_item_id: str | None = None,
    ) -> EngineResult:
        """Process a delegation through the 4-step lifecycle.

        If *work_item_id* is ``None``, uses candidate selection to find
        the top candidate.

        Parameters
        ----------
        work_item_id:
            Optional specific work item to delegate.  If not provided,
            the candidate selector picks the best candidate.

        Returns
        -------
        EngineResult
            Complete audit trail of what happened.
        """
        ts = self._clock()

        # --- Pre-checks: audit_only and hold mode ---
        if self._config.audit_only:
            LOG.info("Engine in audit-only mode, skipping delegation")
            return EngineResult(
                status=EngineStatus.SKIPPED,
                reason="audit_only mode enabled",
                timestamp=ts,
            )

        if self._config.fallback_mode == "hold":
            LOG.info("Fallback mode is 'hold', skipping delegation")
            return EngineResult(
                status=EngineStatus.SKIPPED,
                reason="Fallback mode is 'hold'",
                timestamp=ts,
            )

        if self._config.fallback_mode == "discuss-options":
            LOG.info(
                "Fallback mode is 'discuss-options' (not yet implemented); "
                "falling back to hold — skipping delegation"
            )
            return EngineResult(
                status=EngineStatus.SKIPPED,
                reason="Fallback mode is 'discuss-options' (deferred to hold)",
                timestamp=ts,
            )

        # --- Candidate selection ---
        if work_item_id is None:
            candidate_result = self._selector.select()

            if not candidate_result.selected:
                LOG.info("No candidates for delegation: %s", candidate_result.summary())
                self._notifier.send(
                    "Agents are idle: no actionable items found",
                    title="No Candidates",
                    level="info",
                )
                return EngineResult(
                    status=EngineStatus.NO_CANDIDATES,
                    reason=candidate_result.summary(),
                    candidate_result=candidate_result,
                    timestamp=ts,
                )

            selected = candidate_result.selected
            work_item_id = selected.id
            item_stage = selected.stage
            item_status = selected.status
        else:
            candidate_result = None
            # Fetch work item to get current state
            wi_data = self._fetcher.fetch(work_item_id)
            if wi_data is None:
                return EngineResult(
                    status=EngineStatus.ERROR,
                    reason=f"Failed to fetch work item {work_item_id}",
                    work_item_id=work_item_id,
                    timestamp=ts,
                )
            wi = wi_data.get("workItem", wi_data)
            item_stage = str(wi.get("stage", ""))
            item_status = str(wi.get("status", ""))

        # --- Look up the delegate command ---
        try:
            delegate_cmd = self._descriptor.get_command("delegate")
        except KeyError:
            return EngineResult(
                status=EngineStatus.ERROR,
                reason="No 'delegate' command defined in workflow descriptor",
                work_item_id=work_item_id,
                timestamp=ts,
            )

        # --- Step 1: Confirm from-state ---
        current_state = StateTuple(status=item_status, stage=item_stage)
        valid_from_states = [
            self._descriptor.resolve_state_ref(ref) for ref in delegate_cmd.from_states
        ]
        if current_state not in valid_from_states:
            reason = (
                f"Work item {work_item_id} is in state "
                f"({item_status}, {item_stage}), which is not a valid "
                f"from-state for 'delegate'. "
                f"Valid from-states: {valid_from_states}"
            )
            LOG.warning("Step 1 failed: %s", reason)
            return EngineResult(
                status=EngineStatus.REJECTED,
                reason=reason,
                work_item_id=work_item_id,
                command_name="delegate",
                candidate_result=candidate_result,
                timestamp=ts,
            )

        # --- Step 2: Evaluate pre-invariants ---
        if delegate_cmd.pre:
            # We need the full work item data for invariant evaluation.
            # The assembler's fetcher returns wl show output.
            work_item_data = self._fetch_work_item_data(work_item_id)
            if work_item_data is None:
                return EngineResult(
                    status=EngineStatus.ERROR,
                    reason=f"Failed to fetch work item data for invariant evaluation: {work_item_id}",
                    work_item_id=work_item_id,
                    command_name="delegate",
                    timestamp=ts,
                )

            inv_result = self._evaluator.evaluate(
                delegate_cmd.pre,
                work_item_data,
                fail_fast=False,
            )

            if not inv_result.passed:
                reason = f"Pre-invariant check failed: {inv_result.summary()}"
                LOG.warning("Step 2 failed for %s: %s", work_item_id, reason)

                # Record failure as work item comment
                self._comment_writer.write_comment(
                    work_item_id,
                    f"Delegation blocked: {inv_result.summary()}",
                )

                # Notify
                self._notifier.send(
                    f"Delegation blocked for {work_item_id}: {inv_result.summary()}",
                    title="Pre-Invariant Failure",
                    level="warning",
                )

                return EngineResult(
                    status=EngineStatus.INVARIANT_FAILED,
                    reason=reason,
                    work_item_id=work_item_id,
                    command_name="delegate",
                    invariant_result=inv_result,
                    candidate_result=candidate_result,
                    timestamp=ts,
                )
        else:
            inv_result = None

        # --- Step 3: Apply state transition ---
        to_state = self._descriptor.resolve_state_ref(delegate_cmd.to)

        # Determine assignee from effects
        assignee = None
        if delegate_cmd.effects and delegate_cmd.effects.set_assignee:
            assignee = delegate_cmd.effects.set_assignee

        update_ok = self._updater.update(
            work_item_id,
            status=to_state.status,
            stage=to_state.stage,
            assignee=assignee,
        )
        if not update_ok:
            # Retry once per PRD Section 6.2
            LOG.warning("First wl update failed for %s, retrying...", work_item_id)
            update_ok = self._updater.update(
                work_item_id,
                status=to_state.status,
                stage=to_state.stage,
                assignee=assignee,
            )
            if not update_ok:
                reason = (
                    f"Failed to apply state transition to "
                    f"({to_state.status}, {to_state.stage}) for {work_item_id}"
                )
                LOG.error("Step 3 failed: %s", reason)
                return EngineResult(
                    status=EngineStatus.UPDATE_FAILED,
                    reason=reason,
                    work_item_id=work_item_id,
                    command_name="delegate",
                    timestamp=ts,
                )

        LOG.info(
            "Step 3: Applied transition for %s -> (%s, %s)",
            work_item_id,
            to_state.status,
            to_state.stage,
        )

        # --- Step 4: Execute command logic ---
        # Resolve the matching from-state alias so we can look up the
        # dispatch template from the command's dispatch_map.
        from_alias = self._descriptor.resolve_from_state_alias(
            delegate_cmd, current_state
        )
        action = from_alias  # Used in logs, notifications, EngineResult

        # Apply fallback mode overrides — these affect the proceed/decline
        # decision but do NOT change the dispatch template lookup, which
        # must always use the from-state alias.
        if self._config.fallback_mode == "auto-decline":
            LOG.info("Fallback mode auto-decline: skipping dispatch")
            return EngineResult(
                status=EngineStatus.SKIPPED,
                reason="fallback mode auto-decline",
                work_item_id=work_item_id,
                command_name="delegate",
                action=action,
                timestamp=ts,
            )

        if action is None:
            reason = (
                f"No from-state alias matched state "
                f"({item_status}, {item_stage}) in delegate command"
            )
            LOG.error("Step 4 failed: %s", reason)
            return EngineResult(
                status=EngineStatus.ERROR,
                reason=reason,
                work_item_id=work_item_id,
                command_name="delegate",
                action=action,
                timestamp=ts,
            )

        # Look up the dispatch template from the descriptor
        template = delegate_cmd.dispatch_map.get(action) if action else None
        if template is None:
            reason = (
                f"No dispatch template for from-state alias '{action}' "
                f"in delegate command's dispatch_map"
            )
            LOG.error("Step 4 failed: %s", reason)
            return EngineResult(
                status=EngineStatus.ERROR,
                reason=reason,
                work_item_id=work_item_id,
                command_name="delegate",
                action=action,
                timestamp=ts,
            )

        command_str = template.format(id=work_item_id)

        # Dispatch
        dispatch_result = self._dispatcher.dispatch(
            command=command_str,
            work_item_id=work_item_id,
        )

        if not dispatch_result.success:
            reason = f"Dispatch failed: {dispatch_result.error}"
            LOG.error("Step 4 dispatch failed for %s: %s", work_item_id, reason)

            self._notifier.send(
                f"Dispatch failed for {work_item_id}: {dispatch_result.error}",
                title="Dispatch Failure",
                level="error",
            )

            return EngineResult(
                status=EngineStatus.DISPATCH_FAILED,
                reason=reason,
                work_item_id=work_item_id,
                command_name="delegate",
                action=action,
                dispatch_result=dispatch_result,
                candidate_result=candidate_result,
                timestamp=ts,
            )

        # Record dispatch in audit trail
        dispatch_record = {
            "work_item_id": work_item_id,
            "action": action,
            "command": command_str,
            "pid": dispatch_result.pid,
            "timestamp": dispatch_result.timestamp.isoformat(),
            "status": "dispatched",
        }
        record_id = self._recorder.record_dispatch(dispatch_record)
        if record_id:
            dispatch_record["id"] = record_id

        # Post-dispatch notification
        self._notifier.send(
            f"Delegated '{action}' for {work_item_id} (pid={dispatch_result.pid})",
            title="Delegation Dispatch",
            level="info",
        )

        LOG.info(
            "Engine completed: dispatched %s action=%s pid=%s",
            work_item_id,
            action,
            dispatch_result.pid,
        )

        return EngineResult(
            status=EngineStatus.SUCCESS,
            reason=f"Dispatched '{action}' for {work_item_id}",
            work_item_id=work_item_id,
            command_name="delegate",
            action=action,
            dispatch_result=dispatch_result,
            invariant_result=inv_result if delegate_cmd.pre else None,
            candidate_result=candidate_result,
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # Mode B: Agent callback (state transition request)
    # ------------------------------------------------------------------

    def process_transition(
        self,
        work_item_id: str,
        target_stage: str,
    ) -> EngineResult:
        """Process a state transition request from a delegated agent.

        The agent has completed its work and is requesting a transition
        to *target_stage* (e.g. ``in_review``).  The engine evaluates
        post-invariants for the transition and applies or refuses it.

        Parameters
        ----------
        work_item_id:
            The work item requesting the transition.
        target_stage:
            The target stage (e.g. ``in_review``).

        Returns
        -------
        EngineResult
            Whether the transition was applied or refused.
        """
        ts = self._clock()

        # Fetch current work item state
        work_item_data = self._fetch_work_item_data(work_item_id)
        if work_item_data is None:
            return EngineResult(
                status=EngineStatus.ERROR,
                reason=f"Failed to fetch work item {work_item_id}",
                work_item_id=work_item_id,
                timestamp=ts,
            )

        wi = work_item_data.get("workItem", work_item_data)
        current_status = str(wi.get("status", ""))
        current_stage = str(wi.get("stage", ""))
        current_state = StateTuple(status=current_status, stage=current_stage)

        # Find the command that transitions from current state to target
        target_cmd = self._find_transition_command(current_state, target_stage)
        if target_cmd is None:
            reason = (
                f"No command found for transition from "
                f"({current_status}, {current_stage}) to stage '{target_stage}'"
            )
            LOG.warning("Transition rejected for %s: %s", work_item_id, reason)
            return EngineResult(
                status=EngineStatus.REJECTED,
                reason=reason,
                work_item_id=work_item_id,
                timestamp=ts,
            )

        # Evaluate post-invariants
        if target_cmd.post:
            inv_result = self._evaluator.evaluate(
                target_cmd.post,
                work_item_data,
                fail_fast=False,
            )

            if not inv_result.passed:
                reason = f"Post-invariant check failed: {inv_result.summary()}"
                LOG.warning("Transition refused for %s: %s", work_item_id, reason)

                self._comment_writer.write_comment(
                    work_item_id,
                    f"Transition to '{target_stage}' refused: {inv_result.summary()}",
                )

                return EngineResult(
                    status=EngineStatus.INVARIANT_FAILED,
                    reason=reason,
                    work_item_id=work_item_id,
                    command_name=target_cmd.name,
                    invariant_result=inv_result,
                    timestamp=ts,
                )
        else:
            inv_result = None

        # Apply transition
        to_state = self._descriptor.resolve_state_ref(target_cmd.to)
        update_ok = self._updater.update(
            work_item_id,
            status=to_state.status,
            stage=to_state.stage,
        )
        if not update_ok:
            # Retry once
            update_ok = self._updater.update(
                work_item_id,
                status=to_state.status,
                stage=to_state.stage,
            )
            if not update_ok:
                return EngineResult(
                    status=EngineStatus.UPDATE_FAILED,
                    reason=f"Failed to apply transition to ({to_state.status}, {to_state.stage})",
                    work_item_id=work_item_id,
                    command_name=target_cmd.name,
                    timestamp=ts,
                )

        LOG.info(
            "Transition applied for %s: (%s, %s) -> (%s, %s) via '%s'",
            work_item_id,
            current_status,
            current_stage,
            to_state.status,
            to_state.stage,
            target_cmd.name,
        )

        return EngineResult(
            status=EngineStatus.SUCCESS,
            reason=f"Transition applied via '{target_cmd.name}'",
            work_item_id=work_item_id,
            command_name=target_cmd.name,
            invariant_result=inv_result,
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_work_item_data(self, work_item_id: str) -> dict[str, Any] | None:
        """Fetch work item data via the work item fetcher.

        Returns the raw ``wl show`` JSON or ``None`` on failure.
        """
        try:
            return self._fetcher.fetch(work_item_id)
        except Exception:
            LOG.exception("Failed to fetch work item %s", work_item_id)
            return None

    def _find_transition_command(
        self,
        current_state: StateTuple,
        target_stage: str,
    ) -> Any | None:
        """Find the command that transitions from *current_state* to *target_stage*.

        Returns the matching ``Command`` or ``None``.
        """
        for cmd in self._descriptor.commands.values():
            # Check if current state is in the from-states
            from_states = [
                self._descriptor.resolve_state_ref(ref) for ref in cmd.from_states
            ]
            if current_state not in from_states:
                continue

            # Check if the to-state matches the target stage
            to_state = self._descriptor.resolve_state_ref(cmd.to)
            if to_state.stage == target_stage:
                return cmd

        return None
