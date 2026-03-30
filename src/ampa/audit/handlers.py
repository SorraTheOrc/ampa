"""Descriptor-driven audit command handlers.

Implements the three audit lifecycle commands from ``workflow.yaml``:

- ``audit_result``: Parse audit output, evaluate pre-invariants, transition
  to ``audit_passed``, and post a structured ``# AMPA Audit Result`` comment.
- ``audit_fail``: Record that an audit found unmet acceptance criteria,
  transition to ``audit_failed``, and tag the work item.
- ``close_with_audit``: Close a work item after successful audit, set
  ``needs_producer_review``, tag ``audit_closed``, and send Discord notification.

Each handler follows the engine's 4-step command execution lifecycle:
1. Confirm from-state
2. Evaluate pre-invariants
3. Apply state transition
4. Execute command-specific logic (comment, tags, notification)

Handlers conform to the ``AuditHandoffHandler`` protocol
(``__call__(self, work_item) -> bool``) so they can be plugged directly
into the audit poller's routing chain.

Usage::

    from ampa.audit.handlers import AuditResultHandler

    handler = AuditResultHandler(
        descriptor=descriptor,
        evaluator=evaluator,
        updater=updater,
        comment_writer=comment_writer,
        fetcher=fetcher,
        run_shell=subprocess.run,
    )
    success = handler(work_item)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence

from ampa.audit.result import AuditResult, ParseError, parse_audit_output
from ampa.engine.descriptor import StateTuple, WorkflowDescriptor
from ampa.engine.invariants import InvariantEvaluator, extract_work_item_fields

LOG = logging.getLogger("ampa.audit.handlers")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandlerResult:
    """Typed result from an audit command handler.

    Attributes:
        success: Whether the command executed successfully.
        reason: Machine-readable reason for the outcome.
        command: The command name that was executed.
        work_item_id: The work item ID that was processed.
        details: Additional context for logging/debugging.
    """

    success: bool
    reason: str
    command: str = ""
    work_item_id: str = ""
    details: str = ""


# ---------------------------------------------------------------------------
# Protocols (lightweight — rely on engine protocols for actual impl)
# ---------------------------------------------------------------------------


class _WorkItemUpdater:
    """Protocol-compatible interface for ``WorkItemUpdater``."""

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool: ...


class _CommentWriter:
    """Protocol-compatible interface for ``WorkItemCommentWriter``."""

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool: ...


class _WorkItemFetcher:
    """Protocol-compatible interface for ``WorkItemFetcher``."""

    def fetch(self, work_item_id: str) -> dict[str, Any] | None: ...


class _NotificationSender:
    """Protocol-compatible interface for ``NotificationSender``."""

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_work_item_id(work_item: Dict[str, Any]) -> str:
    """Extract work item ID from a work item dict."""
    wi = work_item.get("workItem", work_item)
    return str(wi.get("id", ""))


def _get_work_item_state(work_item: Dict[str, Any]) -> StateTuple:
    """Extract current (status, stage) from a work item dict."""
    wi = work_item.get("workItem", work_item)
    return StateTuple(
        status=str(wi.get("status", "")),
        stage=str(wi.get("stage", "")),
    )


def _get_work_item_title(work_item: Dict[str, Any]) -> str:
    """Extract title from a work item dict."""
    wi = work_item.get("workItem", work_item)
    return str(wi.get("title", ""))


def _get_work_item_tags(work_item: Dict[str, Any]) -> list[str]:
    """Extract tags from a work item dict."""
    wi = work_item.get("workItem", work_item)
    return wi.get("tags", []) or []


def _check_from_state(
    descriptor: WorkflowDescriptor,
    command_name: str,
    current_state: StateTuple,
) -> HandlerResult | None:
    """Validate the work item is in a valid from-state for the command.

    Returns ``None`` if valid, or a ``HandlerResult`` with failure details.
    """
    cmd = descriptor.get_command(command_name)
    valid_states = [descriptor.resolve_state_ref(ref) for ref in cmd.from_states]
    if current_state not in valid_states:
        expected = ", ".join(f"({s.status}, {s.stage})" for s in valid_states)
        return HandlerResult(
            success=False,
            reason="invalid_from_state",
            command=command_name,
            details=(
                f"Work item is in ({current_state.status}, {current_state.stage}), "
                f"expected one of: {expected}"
            ),
        )
    return None


def _format_audit_comment(audit_result: AuditResult) -> str:
    """Format a structured ``# AMPA Audit Result`` comment.

    Matches the format expected by the ``requires_audit_result`` invariant
    and consumed by downstream handlers.
    """
    parts = ["# AMPA Audit Result", ""]

    if audit_result.summary:
        parts.append(f"## Summary\n\n{audit_result.summary}")
        parts.append("")

    if audit_result.acceptance_criteria:
        parts.append("## Acceptance Criteria Status")
        parts.append("")
        parts.append("| # | Criterion | Verdict | Evidence |")
        parts.append("|---|-----------|---------|----------|")
        for c in audit_result.acceptance_criteria:
            parts.append(f"| {c.number} | {c.criterion} | {c.verdict} | {c.evidence} |")
        parts.append("")

    if audit_result.recommends_closure:
        parts.append("## Recommendation")
        parts.append("")
        if audit_result.closure_reason:
            parts.append(audit_result.closure_reason)
        else:
            parts.append(
                "Can this item be closed? Yes. All acceptance criteria are met."
            )
    else:
        parts.append("## Recommendation")
        parts.append("")
        if audit_result.closure_reason:
            parts.append(audit_result.closure_reason)
        else:
            parts.append("Can this item be closed? No.")

    return "\n".join(parts)


def _apply_tags(
    run_shell: Callable[..., subprocess.CompletedProcess],
    work_item_id: str,
    existing_tags: list[str],
    add_tags: Sequence[str],
    *,
    command_cwd: str | None = None,
    timeout: int = 300,
) -> bool:
    """Add tags to a work item by merging with existing tags.

    Uses ``wl update --tags`` which replaces all tags, so we merge first.
    """
    merged = list(existing_tags)
    for tag in add_tags:
        if tag not in merged:
            merged.append(tag)

    if set(merged) == set(existing_tags):
        return True  # No change needed

    tags_str = ",".join(merged)
    cmd = f'wl update {work_item_id} --tags "{tags_str}" --json'
    try:
        proc = run_shell(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            cwd=command_cwd,
            timeout=timeout,
        )
        if proc.returncode != 0:
            LOG.warning(
                "Failed to add tags to %s: rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return False
        return True
    except Exception:
        LOG.exception("Failed to add tags to %s", work_item_id)
        return False


def _set_needs_producer_review(
    run_shell: Callable[..., subprocess.CompletedProcess],
    work_item_id: str,
    value: bool = True,
    *,
    command_cwd: str | None = None,
    timeout: int = 300,
) -> bool:
    """Set needs-producer-review flag on a work item."""
    flag = "true" if value else "false"
    cmd = f"wl update {work_item_id} --needs-producer-review {flag} --json"
    try:
        proc = run_shell(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            cwd=command_cwd,
            timeout=timeout,
        )
        if proc.returncode != 0:
            LOG.warning(
                "Failed to set needs-producer-review on %s: rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return False
        return True
    except Exception:
        LOG.exception("Failed to set needs-producer-review on %s", work_item_id)
        return False


# ---------------------------------------------------------------------------
# Handler: audit_result
# ---------------------------------------------------------------------------


class AuditResultHandler:
    """Handler for the ``audit_result`` command.

    Lifecycle:
    1. Confirm work item is in ``review`` state.
    2. Run the audit skill and parse output into ``AuditResult``.
    3. Evaluate ``requires_audit_result`` pre-invariant against the
       work item (with the audit comment already posted).
    4. Transition to ``audit_passed`` state.
    5. Post structured ``# AMPA Audit Result`` comment.

    Note: This handler runs the audit skill internally via
    ``opencode run "/audit {id}"``.  The audit output is parsed by
    ``parse_audit_output()`` from ``ampa.audit.result``.

    Parameters
    ----------
    descriptor:
        Workflow descriptor for command/state definitions.
    evaluator:
        Invariant evaluator for pre-invariant checks.
    updater:
        Work item state updater (``wl update``).
    comment_writer:
        Comment writer (``wl comment add``).
    fetcher:
        Work item fetcher (``wl show``), used to re-fetch after comment.
    run_shell:
        Shell runner for ``opencode run`` calls.
    command_cwd:
        Working directory for shell commands.
    """

    def __init__(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: Any,  # WorkItemUpdater protocol
        comment_writer: Any,  # WorkItemCommentWriter protocol
        fetcher: Any,  # WorkItemFetcher protocol
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._evaluator = evaluator
        self._updater = updater
        self._comment_writer = comment_writer
        self._fetcher = fetcher
        self._run_shell = run_shell
        self._cwd = command_cwd

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        """Execute the ``audit_result`` command.

        Returns ``True`` on success, ``False`` on failure.
        """
        result = self.execute(work_item)
        if not result.success:
            LOG.warning(
                "audit_result failed for %s: %s — %s",
                result.work_item_id,
                result.reason,
                result.details,
            )
        return result.success

    def execute(self, work_item: Dict[str, Any]) -> HandlerResult:
        """Execute with full result details.

        This is the main entry point for testing and structured error
        handling.  ``__call__`` delegates to this method.
        """
        work_item_id = _get_work_item_id(work_item)
        if not work_item_id:
            return HandlerResult(
                success=False,
                reason="missing_work_item_id",
                command="audit_result",
            )

        current_state = _get_work_item_state(work_item)

        # Step 1: Confirm from-state
        state_check = _check_from_state(self._descriptor, "audit_result", current_state)
        if state_check is not None:
            return HandlerResult(
                success=False,
                reason=state_check.reason,
                command="audit_result",
                work_item_id=work_item_id,
                details=state_check.details,
            )

        # Step 2: Run audit skill
        audit_result = self._run_audit(work_item_id)
        if isinstance(audit_result, ParseError):
            return HandlerResult(
                success=False,
                reason="audit_parse_error",
                command="audit_result",
                work_item_id=work_item_id,
                details=audit_result.reason,
            )

        # Step 3: Post structured comment (before invariant check,
        # since the invariant checks for the comment's existence)
        comment_text = _format_audit_comment(audit_result)
        comment_ok = self._comment_writer.write_comment(
            work_item_id, comment_text, author="ampa-scheduler"
        )
        if not comment_ok:
            return HandlerResult(
                success=False,
                reason="comment_write_failed",
                command="audit_result",
                work_item_id=work_item_id,
                details="Failed to write audit result comment",
            )

        # Step 4: Re-fetch work item to include new comment for invariant eval
        refreshed = self._fetcher.fetch(work_item_id)
        if refreshed is None:
            LOG.warning(
                "Failed to re-fetch %s after comment; proceeding with original data",
                work_item_id,
            )
            refreshed = work_item

        # Step 5: Evaluate pre-invariants
        cmd = self._descriptor.get_command("audit_result")
        if cmd.pre:
            inv_result = self._evaluator.evaluate(
                list(cmd.pre), refreshed, fail_fast=True
            )
            if not inv_result.passed:
                return HandlerResult(
                    success=False,
                    reason="pre_invariant_failed",
                    command="audit_result",
                    work_item_id=work_item_id,
                    details=inv_result.summary(),
                )

        # Step 6: If audit does not recommend closure, do not transition here.
        # The caller can route to the audit_fail command while item is still in
        # review state.
        if not audit_result.recommends_closure:
            return HandlerResult(
                success=True,
                reason="audit_recommends_no_closure",
                command="audit_result",
                work_item_id=work_item_id,
                details="Audit completed but does not recommend closure",
            )

        # Step 7: Apply state transition to audit_passed
        to_state = self._descriptor.resolve_state_ref(cmd.to)
        update_ok = self._updater.update(
            work_item_id,
            status=to_state.status,
            stage=to_state.stage,
        )
        if not update_ok:
            return HandlerResult(
                success=False,
                reason="state_transition_failed",
                command="audit_result",
                work_item_id=work_item_id,
                details=f"Failed to transition to ({to_state.status}, {to_state.stage})",
            )

        LOG.info(
            "audit_result succeeded for %s: recommends_closure=%s",
            work_item_id,
            audit_result.recommends_closure,
        )

        return HandlerResult(
            success=True,
            reason="audit_result_recorded",
            command="audit_result",
            work_item_id=work_item_id,
            details=(
                f"recommends_closure={audit_result.recommends_closure}, "
                f"criteria={len(audit_result.acceptance_criteria)}"
            ),
        )

    def _run_audit(self, work_item_id: str) -> AuditResult | ParseError:
        """Execute ``opencode run "/audit {id}"`` and parse the output."""
        cmd = f'opencode run "/audit {work_item_id}"'
        try:
            proc = self._run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=600,  # audit can take a while
            )
        except subprocess.TimeoutExpired:
            return ParseError(
                reason=f"Audit skill timed out for {work_item_id}",
                raw_output="",
            )
        except Exception as exc:
            return ParseError(
                reason=f"Audit skill execution failed: {exc}",
                raw_output="",
            )

        if proc.returncode != 0:
            LOG.warning(
                "opencode run /audit %s returned rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            # Still try to parse — the output may contain useful data

        raw_output = proc.stdout or ""
        return parse_audit_output(raw_output)


# ---------------------------------------------------------------------------
# Handler: audit_fail
# ---------------------------------------------------------------------------


class AuditFailHandler:
    """Handler for the ``audit_fail`` command.

    Lifecycle:
    1. Confirm work item is in ``review`` state.
    2. Evaluate pre-invariants (``requires_audit_result``,
       ``audit_does_not_recommend_closure``).
    3. Transition to ``audit_failed`` state.
    4. Apply effects: ``add_tags: [audit_failed]``.

    This handler is called after ``audit_result`` has already been recorded
    and the audit output does NOT recommend closure.

    Parameters
    ----------
    descriptor:
        Workflow descriptor for command/state definitions.
    evaluator:
        Invariant evaluator for pre-invariant checks.
    updater:
        Work item state updater (``wl update``).
    run_shell:
        Shell runner for tag updates.
    command_cwd:
        Working directory for shell commands.
    """

    def __init__(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: Any,  # WorkItemUpdater protocol
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._evaluator = evaluator
        self._updater = updater
        self._run_shell = run_shell
        self._cwd = command_cwd

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        """Execute the ``audit_fail`` command."""
        result = self.execute(work_item)
        if not result.success:
            LOG.warning(
                "audit_fail failed for %s: %s — %s",
                result.work_item_id,
                result.reason,
                result.details,
            )
        return result.success

    def execute(self, work_item: Dict[str, Any]) -> HandlerResult:
        """Execute with full result details."""
        work_item_id = _get_work_item_id(work_item)
        if not work_item_id:
            return HandlerResult(
                success=False,
                reason="missing_work_item_id",
                command="audit_fail",
            )

        current_state = _get_work_item_state(work_item)

        # Step 1: Confirm from-state
        state_check = _check_from_state(self._descriptor, "audit_fail", current_state)
        if state_check is not None:
            return HandlerResult(
                success=False,
                reason=state_check.reason,
                command="audit_fail",
                work_item_id=work_item_id,
                details=state_check.details,
            )

        # Step 2: Evaluate pre-invariants
        cmd = self._descriptor.get_command("audit_fail")
        if cmd.pre:
            inv_result = self._evaluator.evaluate(
                list(cmd.pre), work_item, fail_fast=True
            )
            if not inv_result.passed:
                return HandlerResult(
                    success=False,
                    reason="pre_invariant_failed",
                    command="audit_fail",
                    work_item_id=work_item_id,
                    details=inv_result.summary(),
                )

        # Step 3: Apply state transition to audit_failed
        to_state = self._descriptor.resolve_state_ref(cmd.to)
        update_ok = self._updater.update(
            work_item_id,
            status=to_state.status,
            stage=to_state.stage,
        )
        if not update_ok:
            return HandlerResult(
                success=False,
                reason="state_transition_failed",
                command="audit_fail",
                work_item_id=work_item_id,
                details=f"Failed to transition to ({to_state.status}, {to_state.stage})",
            )

        # Step 4: Apply effects — add_tags
        if cmd.effects and cmd.effects.add_tags:
            existing_tags = _get_work_item_tags(work_item)
            tag_ok = _apply_tags(
                self._run_shell,
                work_item_id,
                existing_tags,
                cmd.effects.add_tags,
                command_cwd=self._cwd,
            )
            if not tag_ok:
                LOG.warning(
                    "Failed to apply tags %s to %s (non-fatal)",
                    cmd.effects.add_tags,
                    work_item_id,
                )

        LOG.info("audit_fail succeeded for %s", work_item_id)

        return HandlerResult(
            success=True,
            reason="audit_fail_recorded",
            command="audit_fail",
            work_item_id=work_item_id,
        )


# ---------------------------------------------------------------------------
# Handler: close_with_audit
# ---------------------------------------------------------------------------


class CloseWithAuditHandler:
    """Handler for the ``close_with_audit`` command.

    Lifecycle:
    1. Confirm work item is in ``audit_passed`` state.
    2. Evaluate pre-invariant (``audit_recommends_closure``).
    3. Transition to ``{status: completed, stage: in_review}``.
    4. Apply effects:
       - ``set_needs_producer_review: true``
       - ``add_tags: [audit_closed]``
       - Discord notification

    Parameters
    ----------
    descriptor:
        Workflow descriptor for command/state definitions.
    evaluator:
        Invariant evaluator for pre-invariant checks.
    updater:
        Work item state updater (``wl update``).
    notifier:
        Notification sender for Discord messages.
    run_shell:
        Shell runner for tag/flag updates.
    command_cwd:
        Working directory for shell commands.
    """

    def __init__(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: Any,  # WorkItemUpdater protocol
        notifier: Any,  # NotificationSender protocol
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._evaluator = evaluator
        self._updater = updater
        self._notifier = notifier
        self._run_shell = run_shell
        self._cwd = command_cwd

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        """Execute the ``close_with_audit`` command."""
        result = self.execute(work_item)
        if not result.success:
            LOG.warning(
                "close_with_audit failed for %s: %s — %s",
                result.work_item_id,
                result.reason,
                result.details,
            )
        return result.success

    def execute(self, work_item: Dict[str, Any]) -> HandlerResult:
        """Execute with full result details."""
        work_item_id = _get_work_item_id(work_item)
        if not work_item_id:
            return HandlerResult(
                success=False,
                reason="missing_work_item_id",
                command="close_with_audit",
            )

        current_state = _get_work_item_state(work_item)

        # Step 1: Confirm from-state
        state_check = _check_from_state(
            self._descriptor, "close_with_audit", current_state
        )
        if state_check is not None:
            return HandlerResult(
                success=False,
                reason=state_check.reason,
                command="close_with_audit",
                work_item_id=work_item_id,
                details=state_check.details,
            )

        # Step 2: Evaluate pre-invariants
        cmd = self._descriptor.get_command("close_with_audit")
        if cmd.pre:
            inv_result = self._evaluator.evaluate(
                list(cmd.pre), work_item, fail_fast=True
            )
            if not inv_result.passed:
                return HandlerResult(
                    success=False,
                    reason="pre_invariant_failed",
                    command="close_with_audit",
                    work_item_id=work_item_id,
                    details=inv_result.summary(),
                )

        # Step 2b: Auto-completion checks from acceptance criteria.
        check_failures = self._run_completion_checks(work_item)
        if check_failures:
            return HandlerResult(
                success=False,
                reason="completion_checks_failed",
                command="close_with_audit",
                work_item_id=work_item_id,
                details=", ".join(check_failures),
            )

        # Step 3: Apply state transition
        to_state = self._descriptor.resolve_state_ref(cmd.to)
        update_ok = self._updater.update(
            work_item_id,
            status=to_state.status,
            stage=to_state.stage,
        )
        if not update_ok:
            return HandlerResult(
                success=False,
                reason="state_transition_failed",
                command="close_with_audit",
                work_item_id=work_item_id,
                details=f"Failed to transition to ({to_state.status}, {to_state.stage})",
            )

        # Step 4a: set_needs_producer_review
        if cmd.effects and cmd.effects.set_needs_producer_review:
            npr_ok = _set_needs_producer_review(
                self._run_shell,
                work_item_id,
                value=True,
                command_cwd=self._cwd,
            )
            if not npr_ok:
                LOG.warning(
                    "Failed to set needs-producer-review on %s (non-fatal)",
                    work_item_id,
                )

        # Step 4b: add_tags
        if cmd.effects and cmd.effects.add_tags:
            existing_tags = _get_work_item_tags(work_item)
            tag_ok = _apply_tags(
                self._run_shell,
                work_item_id,
                existing_tags,
                cmd.effects.add_tags,
                command_cwd=self._cwd,
            )
            if not tag_ok:
                LOG.warning(
                    "Failed to apply tags %s to %s (non-fatal)",
                    cmd.effects.add_tags,
                    work_item_id,
                )

        # Step 4c: Discord notification
        if cmd.effects and cmd.effects.notifications:
            title = _get_work_item_title(work_item)
            for notif in cmd.effects.notifications:
                if notif.channel == "discord":
                    msg = notif.message.replace("${title}", title)
                    try:
                        self._notifier.send(msg, title=msg, level="info")
                    except Exception:
                        LOG.exception(
                            "Failed to send Discord notification for %s",
                            work_item_id,
                        )

        LOG.info("close_with_audit succeeded for %s", work_item_id)

        return HandlerResult(
            success=True,
            reason="close_with_audit_completed",
            command="close_with_audit",
            work_item_id=work_item_id,
        )

    def _run_completion_checks(self, work_item: Dict[str, Any]) -> list[str]:
        """Run pre-close checks required by close_with_audit.

        Returns a list of failed check names. Empty list means all checks pass.
        """
        failures: list[str] = []
        work_item_id = _get_work_item_id(work_item)

        # Check 1: PR merged (via gh pr view) when PR URL can be found.
        pr = self._extract_pr_from_work_item(work_item)
        if pr is None:
            failures.append("pr_not_found")
        else:
            owner_repo, pr_num = pr
            if not self._is_pr_merged(owner_repo, pr_num):
                failures.append("pr_not_merged")

        # Check 2: no open children
        if not self._has_no_open_children(work_item_id):
            failures.append("open_children_exist")

        return failures

    def _extract_pr_from_work_item(
        self, work_item: Dict[str, Any]
    ) -> tuple[str, str] | None:
        """Extract owner/repo and PR number from work item comments."""
        comments = work_item.get("comments")
        if not isinstance(comments, list):
            comments = []
        pattern = re.compile(
            r"https?://github\.com/(?P<owner_repo>[^/]+/[^/]+)/pull/(?P<number>\d+)",
            re.I,
        )
        for comment in reversed(comments):
            if not isinstance(comment, dict):
                continue
            body = (
                comment.get("comment") or comment.get("body") or comment.get("text") or ""
            )
            if not isinstance(body, str):
                continue
            m = pattern.search(body)
            if m:
                return m.group("owner_repo"), m.group("number")
        return None

    def _is_pr_merged(self, owner_repo: str, pr_num: str) -> bool:
        """Check PR merged status via gh CLI."""
        cmd = f"gh pr view {pr_num} --repo {owner_repo} --json merged"
        try:
            proc = self._run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=300,
            )
        except Exception:
            LOG.exception("Failed to execute gh pr view for PR %s", pr_num)
            return False

        if proc.returncode != 0:
            LOG.warning(
                "gh pr view failed for PR %s: rc=%s stderr=%r",
                pr_num,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return False

        try:
            data = json.loads(proc.stdout or "{}")
        except Exception:
            LOG.exception("Failed to parse gh pr view output for PR %s", pr_num)
            return False
        return bool(data.get("merged"))

    def _has_no_open_children(self, work_item_id: str) -> bool:
        """Return True when all direct children are closed/completed."""
        cmd = f"wl show {work_item_id} --children --json"
        try:
            proc = self._run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=300,
            )
        except Exception:
            LOG.exception("Failed to query children for %s", work_item_id)
            return False

        if proc.returncode != 0:
            LOG.warning(
                "wl show --children failed for %s: rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return False

        try:
            payload = json.loads(proc.stdout or "{}")
        except Exception:
            LOG.exception("Failed to parse wl show --children output for %s", work_item_id)
            return False

        children = payload.get("children")
        if not isinstance(children, list):
            return True
        closed_statuses = {"closed", "done", "completed", "resolved"}
        for child in children:
            if not isinstance(child, dict):
                continue
            status = str(child.get("status") or "").strip().lower()
            if status and status not in closed_statuses:
                return False
        return True
