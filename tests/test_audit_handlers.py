"""Unit tests for ampa.audit.handlers — audit command handlers.

Tests the three descriptor-driven audit handlers:
- AuditResultHandler: audit_result command
- AuditFailHandler: audit_fail command
- CloseWithAuditHandler: close_with_audit command

All tests mock external dependencies (shell, wl CLI, opencode run)
and verify the 4-step command execution lifecycle.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from ampa.audit.handlers import (
    AuditFailHandler,
    AuditResultHandler,
    CloseWithAuditHandler,
    HandlerResult,
    _apply_tags,
    _check_from_state,
    _format_audit_comment,
    _get_work_item_id,
    _get_work_item_state,
    _set_needs_producer_review,
)
from ampa.audit.result import (
    AUDIT_REPORT_END,
    AUDIT_REPORT_START,
    AuditResult,
    CriterionResult,
    ParseError,
)
from ampa.engine.descriptor import (
    Effects,
    Notification,
    StateTuple,
    WorkflowDescriptor,
    load_descriptor,
)
from ampa.engine.invariants import InvariantEvaluator, NullQuerier


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def descriptor() -> WorkflowDescriptor:
    """Load the real workflow descriptor."""
    return load_descriptor(
        REPO_ROOT / "docs" / "workflow" / "workflow.yaml",
        schema_path=REPO_ROOT / "docs" / "workflow" / "workflow-schema.json",
    )


@pytest.fixture
def evaluator(descriptor: WorkflowDescriptor) -> InvariantEvaluator:
    """Build evaluator from the real workflow descriptor."""
    return InvariantEvaluator(descriptor.invariants, querier=NullQuerier())


def _make_work_item(
    work_item_id: str = "TEST-001",
    title: str = "Test work item",
    status: str = "in_progress",
    stage: str = "in_review",
    tags: list[str] | None = None,
    comments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a mock work item dict (wl show --json shape)."""
    return {
        "workItem": {
            "id": work_item_id,
            "title": title,
            "description": "Test description",
            "status": status,
            "stage": stage,
            "tags": tags or [],
            "assignee": "",
            "priority": "medium",
        },
        "comments": comments or [],
    }


def _make_audit_output(recommends_closure: bool = True) -> str:
    """Build a realistic audit skill output string."""
    if recommends_closure:
        criteria = (
            "| 1 | Feature works | met | tests pass |\n"
            "| 2 | Documentation | met | README updated |"
        )
        recommendation = (
            "Can this item be closed? Yes. All acceptance criteria are met."
        )
    else:
        criteria = (
            "| 1 | Feature works | met | tests pass |\n"
            "| 2 | Documentation | unmet | README missing |"
        )
        recommendation = "Can this item be closed? No. Documentation is missing."

    return (
        f"Running audit...\n"
        f"{AUDIT_REPORT_START}\n"
        f"## Summary\n\n"
        f"Audit of TEST-001.\n\n"
        f"## Acceptance Criteria Status\n\n"
        f"| # | Criterion | Verdict | Evidence |\n"
        f"|---|-----------|---------|----------|\n"
        f"{criteria}\n\n"
        f"## Recommendation\n\n"
        f"{recommendation}\n"
        f"{AUDIT_REPORT_END}\n"
    )


class MockUpdater:
    """Mock WorkItemUpdater."""

    def __init__(self, succeed: bool = True):
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "status": status,
                "stage": stage,
                "assignee": assignee,
            }
        )
        return self._succeed


class MockCommentWriter:
    """Mock WorkItemCommentWriter."""

    def __init__(self, succeed: bool = True):
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "comment": comment,
                "author": author,
            }
        )
        return self._succeed


class MockFetcher:
    """Mock WorkItemFetcher."""

    def __init__(self, result: dict[str, Any] | None = None):
        self._result = result
        self.calls: list[str] = []

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        self.calls.append(work_item_id)
        return self._result


class MockNotifier:
    """Mock NotificationSender."""

    def __init__(self, succeed: bool = True):
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        self.calls.append({"message": message, "title": title, "level": level})
        return self._succeed


def _mock_shell_success(stdout: str = "", returncode: int = 0):
    """Create a mock run_shell that returns success."""

    def run_shell(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else "",
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return run_shell


def _mock_shell_close_success():
    """Mock shell for close_with_audit checks (gh + children)."""

    def run_shell(*args, **kwargs):
        cmd = args[0] if args else ""
        cmd_s = str(cmd)
        if cmd_s.startswith("gh pr view"):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"merged": true}',
                stderr="",
            )
        if "wl show" in cmd_s and "--children --json" in cmd_s:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"children": []}',
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    return run_shell


def _mock_shell_audit(recommends_closure: bool = True):
    """Create a mock run_shell that returns audit output for opencode run."""
    audit_output = _make_audit_output(recommends_closure)

    def run_shell(*args, **kwargs):
        cmd = args[0] if args else ""
        if "opencode run" in str(cmd):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=audit_output, stderr=""
            )
        # Default success for other commands (wl update, wl comment, etc.)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    return run_shell


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_work_item_id(self) -> None:
        wi = _make_work_item(work_item_id="WL-123")
        assert _get_work_item_id(wi) == "WL-123"

    def test_get_work_item_id_flat(self) -> None:
        wi = {"id": "WL-456", "title": "Flat"}
        assert _get_work_item_id(wi) == "WL-456"

    def test_get_work_item_state(self) -> None:
        wi = _make_work_item(status="in_progress", stage="in_review")
        state = _get_work_item_state(wi)
        assert state == StateTuple(status="in_progress", stage="in_review")

    def test_check_from_state_valid(self, descriptor: WorkflowDescriptor) -> None:
        current = StateTuple(status="in_progress", stage="in_review")
        result = _check_from_state(descriptor, "audit_result", current)
        assert result is None  # valid

    def test_check_from_state_invalid(self, descriptor: WorkflowDescriptor) -> None:
        current = StateTuple(status="open", stage="idea")
        result = _check_from_state(descriptor, "audit_result", current)
        assert result is not None
        assert result.reason == "invalid_from_state"

    def test_format_audit_comment_pass(self) -> None:
        ar = AuditResult(
            summary="All good.",
            acceptance_criteria=(CriterionResult("1", "Feature", "met", "tests pass"),),
            recommends_closure=True,
            closure_reason="Can this item be closed? Yes.",
        )
        comment = _format_audit_comment(ar)
        assert "# AMPA Audit Result" in comment
        assert "All good." in comment
        assert "| 1 | Feature | met | tests pass |" in comment
        assert "Can this item be closed? Yes." in comment

    def test_format_audit_comment_fail(self) -> None:
        ar = AuditResult(
            summary="Issues found.",
            acceptance_criteria=(CriterionResult("1", "Feature", "unmet", "missing"),),
            recommends_closure=False,
            closure_reason="Can this item be closed? No.",
        )
        comment = _format_audit_comment(ar)
        assert "# AMPA Audit Result" in comment
        assert "Can this item be closed? No." in comment

    def test_format_audit_comment_no_criteria(self) -> None:
        ar = AuditResult(summary="Summary only.", recommends_closure=False)
        comment = _format_audit_comment(ar)
        assert "# AMPA Audit Result" in comment
        assert "Summary only." in comment
        assert "Acceptance Criteria" not in comment


# ---------------------------------------------------------------------------
# Tests: HandlerResult
# ---------------------------------------------------------------------------


class TestHandlerResult:
    def test_success_result(self) -> None:
        r = HandlerResult(success=True, reason="ok", command="audit_result")
        assert r.success is True
        assert r.reason == "ok"

    def test_failure_result(self) -> None:
        r = HandlerResult(
            success=False,
            reason="invalid_from_state",
            command="audit_result",
            work_item_id="WL-1",
            details="Wrong state",
        )
        assert r.success is False
        assert r.work_item_id == "WL-1"


# ---------------------------------------------------------------------------
# Tests: AuditResultHandler
# ---------------------------------------------------------------------------


class TestAuditResultHandler:
    def _make_handler(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: MockUpdater | None = None,
        comment_writer: MockCommentWriter | None = None,
        fetcher: MockFetcher | None = None,
        run_shell=None,
    ) -> AuditResultHandler:
        # Default fetcher returns work item with audit comment (for invariant)
        if fetcher is None:
            fetcher = MockFetcher(
                _make_work_item(
                    comments=[
                        {
                            "comment": "# AMPA Audit Result\n\nCan this item be closed? Yes."
                        }
                    ],
                )
            )
        return AuditResultHandler(
            descriptor=descriptor,
            evaluator=evaluator,
            updater=updater or MockUpdater(),
            comment_writer=comment_writer or MockCommentWriter(),
            fetcher=fetcher,
            run_shell=run_shell or _mock_shell_audit(recommends_closure=True),
        )

    def test_success_path(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        updater = MockUpdater()
        comment_writer = MockCommentWriter()
        handler = self._make_handler(
            descriptor, evaluator, updater=updater, comment_writer=comment_writer
        )
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is True
        assert result.reason == "audit_result_recorded"
        # Verify state transition was called
        assert len(updater.calls) == 1
        assert updater.calls[0]["status"] == "completed"
        assert updater.calls[0]["stage"] == "audit_passed"
        # Verify comment was posted
        assert len(comment_writer.calls) == 1
        assert "AMPA Audit Result" in comment_writer.calls[0]["comment"]

    def test_callable_returns_bool(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="in_progress", stage="in_review")
        assert handler(wi) is True

    def test_invalid_from_state(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="open", stage="idea")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "invalid_from_state"

    def test_missing_work_item_id(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = {"workItem": {"title": "No ID"}}
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "missing_work_item_id"

    def test_audit_parse_error(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """When opencode run returns empty output, handler returns parse error."""

        def bad_shell(*args, **kwargs):
            return subprocess.CompletedProcess(
                args="", returncode=0, stdout="", stderr=""
            )

        handler = self._make_handler(descriptor, evaluator, run_shell=bad_shell)
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "audit_parse_error"

    def test_comment_write_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(
            descriptor, evaluator, comment_writer=MockCommentWriter(succeed=False)
        )
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "comment_write_failed"

    def test_state_transition_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(
            descriptor, evaluator, updater=MockUpdater(succeed=False)
        )
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "state_transition_failed"

    def test_audit_timeout(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        def timeout_shell(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="opencode run", timeout=600)

        handler = self._make_handler(descriptor, evaluator, run_shell=timeout_shell)
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "audit_parse_error"
        assert "timed out" in result.details.lower()

    def test_pre_invariant_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """When re-fetched work item has no audit comment, pre-invariant fails."""
        # Fetcher returns work item without audit comment
        fetcher = MockFetcher(_make_work_item(comments=[]))
        handler = self._make_handler(descriptor, evaluator, fetcher=fetcher)
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "pre_invariant_failed"

    def test_no_exceptions_on_expected_errors(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Handler should never raise for expected error conditions."""
        handler = self._make_handler(descriptor, evaluator)
        # Various invalid inputs — none should raise
        for wi in [
            {},
            {"workItem": {}},
            _make_work_item(status="closed", stage="done"),
        ]:
            result = handler.execute(wi)
            assert isinstance(result, HandlerResult)
            # Either missing_work_item_id or invalid_from_state


# ---------------------------------------------------------------------------
# Tests: AuditFailHandler
# ---------------------------------------------------------------------------


class TestAuditFailHandler:
    def _make_handler(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: MockUpdater | None = None,
        run_shell=None,
    ) -> AuditFailHandler:
        return AuditFailHandler(
            descriptor=descriptor,
            evaluator=evaluator,
            updater=updater or MockUpdater(),
            run_shell=run_shell or _mock_shell_success(),
        )

    def test_success_path(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        updater = MockUpdater()
        handler = self._make_handler(descriptor, evaluator, updater=updater)
        wi = _make_work_item(
            status="in_progress",
            stage="in_review",
            comments=[
                {"comment": "AMPA Audit Result: gaps found."},
                {"comment": "Can this item be closed? No."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is True
        assert result.reason == "audit_fail_recorded"
        # Verify state transition
        assert len(updater.calls) == 1
        assert updater.calls[0]["status"] == "in_progress"
        assert updater.calls[0]["stage"] == "audit_failed"

    def test_callable_returns_bool(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(
            status="in_progress",
            stage="in_review",
            comments=[
                {"comment": "AMPA Audit Result: gaps."},
                {"comment": "Can this item be closed? No."},
            ],
        )
        assert handler(wi) is True

    def test_invalid_from_state(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="open", stage="idea")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "invalid_from_state"

    def test_pre_invariant_failure_no_audit(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Fails if no audit result comment exists."""
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="in_progress", stage="in_review", comments=[])
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "pre_invariant_failed"

    def test_pre_invariant_failure_recommends_closure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Fails if audit recommends closure (wrong invariant match)."""
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(
            status="in_progress",
            stage="in_review",
            comments=[
                {"comment": "AMPA Audit Result: all good."},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        result = handler.execute(wi)
        # audit_does_not_recommend_closure should fail since the comment says "yes"
        assert result.success is False
        assert result.reason == "pre_invariant_failed"

    def test_state_transition_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(
            descriptor, evaluator, updater=MockUpdater(succeed=False)
        )
        wi = _make_work_item(
            status="in_progress",
            stage="in_review",
            comments=[
                {"comment": "AMPA Audit Result: gaps."},
                {"comment": "Can this item be closed? No."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "state_transition_failed"

    def test_tag_application(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Verify that audit_failed tag is applied via shell."""
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            cmd = args[0] if args else ""
            shell_calls.append(str(cmd))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )

        handler = self._make_handler(descriptor, evaluator, run_shell=tracking_shell)
        wi = _make_work_item(
            status="in_progress",
            stage="in_review",
            comments=[
                {"comment": "AMPA Audit Result: gaps."},
                {"comment": "Can this item be closed? No."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is True
        # Check that a wl update --tags command was called
        tag_cmds = [c for c in shell_calls if "--tags" in c]
        assert len(tag_cmds) == 1
        assert "audit_failed" in tag_cmds[0]


# ---------------------------------------------------------------------------
# Tests: CloseWithAuditHandler
# ---------------------------------------------------------------------------


class TestCloseWithAuditHandler:
    def _make_handler(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        updater: MockUpdater | None = None,
        notifier: MockNotifier | None = None,
        run_shell=None,
    ) -> CloseWithAuditHandler:
        return CloseWithAuditHandler(
            descriptor=descriptor,
            evaluator=evaluator,
            updater=updater or MockUpdater(),
            notifier=notifier or MockNotifier(),
            run_shell=run_shell or _mock_shell_close_success(),
        )

    def test_success_path(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        updater = MockUpdater()
        notifier = MockNotifier()
        handler = self._make_handler(
            descriptor, evaluator, updater=updater, notifier=notifier
        )
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            title="My Feature",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {
                    "comment": "Can this item be closed? Yes. All acceptance criteria are met."
                },
            ],
        )
        result = handler.execute(wi)
        assert result.success is True
        assert result.reason == "close_with_audit_completed"
        # Verify state transition
        assert len(updater.calls) == 1
        assert updater.calls[0]["status"] == "completed"
        assert updater.calls[0]["stage"] == "in_review"
        # Verify Discord notification was sent
        assert len(notifier.calls) == 1
        assert "My Feature" in notifier.calls[0]["message"]

    def test_callable_returns_bool(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        assert handler(wi) is True

    def test_invalid_from_state(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="in_progress", stage="in_review")
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "invalid_from_state"

    def test_pre_invariant_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Fails if audit does not recommend closure."""
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "Can this item be closed? No."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "pre_invariant_failed"

    def test_needs_producer_review_set(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Verify needs-producer-review flag is set via shell."""
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            cmd = args[0] if args else ""
            shell_calls.append(str(cmd))
            cmd_s = str(cmd)
            if cmd_s.startswith("gh pr view"):
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"merged": true}',
                    stderr="",
                )
            if "wl show" in cmd_s and "--children --json" in cmd_s:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"children": []}',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )

        handler = self._make_handler(descriptor, evaluator, run_shell=tracking_shell)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is True
        npr_cmds = [c for c in shell_calls if "--needs-producer-review" in c]
        assert len(npr_cmds) == 1
        assert "true" in npr_cmds[0]

    def test_audit_closed_tag_applied(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Verify audit_closed tag is applied via shell."""
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            cmd = args[0] if args else ""
            shell_calls.append(str(cmd))
            cmd_s = str(cmd)
            if cmd_s.startswith("gh pr view"):
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"merged": true}',
                    stderr="",
                )
            if "wl show" in cmd_s and "--children --json" in cmd_s:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='{"children": []}',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )

        handler = self._make_handler(descriptor, evaluator, run_shell=tracking_shell)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is True
        tag_cmds = [c for c in shell_calls if "--tags" in c]
        assert len(tag_cmds) == 1
        assert "audit_closed" in tag_cmds[0]

    def test_state_transition_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(
            descriptor, evaluator, updater=MockUpdater(succeed=False)
        )
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "state_transition_failed"

    def test_discord_notification_content(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Verify Discord notification message includes title."""
        notifier = MockNotifier()
        handler = self._make_handler(descriptor, evaluator, notifier=notifier)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            title="Implement feature X",
            comments=[
                {"comment": "PR: https://github.com/example/repo/pull/42"},
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        handler.execute(wi)
        assert len(notifier.calls) == 1
        assert "Implement feature X" in notifier.calls[0]["message"]

    def test_completion_checks_fail_when_pr_missing(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        handler = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(
            status="completed",
            stage="audit_passed",
            comments=[
                {"comment": "Can this item be closed? Yes."},
            ],
        )
        result = handler.execute(wi)
        assert result.success is False
        assert result.reason == "completion_checks_failed"
        assert "pr_not_found" in result.details


# ---------------------------------------------------------------------------
# Tests: _apply_tags and _set_needs_producer_review helpers
# ---------------------------------------------------------------------------


class TestApplyTags:
    def test_adds_new_tag(self) -> None:
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            shell_calls.append(str(args[0] if args else ""))
            return subprocess.CompletedProcess(
                args="", returncode=0, stdout="", stderr=""
            )

        ok = _apply_tags(tracking_shell, "WL-1", ["existing"], ["new_tag"])
        assert ok is True
        assert len(shell_calls) == 1
        assert "existing,new_tag" in shell_calls[0]

    def test_no_op_when_tag_exists(self) -> None:
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            shell_calls.append(str(args[0] if args else ""))
            return subprocess.CompletedProcess(
                args="", returncode=0, stdout="", stderr=""
            )

        ok = _apply_tags(tracking_shell, "WL-1", ["audit_failed"], ["audit_failed"])
        assert ok is True
        # No shell call needed since tag already exists
        assert len(shell_calls) == 0

    def test_shell_failure(self) -> None:
        def failing_shell(*args, **kwargs):
            return subprocess.CompletedProcess(
                args="", returncode=1, stdout="", stderr="error"
            )

        ok = _apply_tags(failing_shell, "WL-1", [], ["new_tag"])
        assert ok is False


class TestSetNeedsProducerReview:
    def test_sets_true(self) -> None:
        shell_calls: list[str] = []

        def tracking_shell(*args, **kwargs):
            shell_calls.append(str(args[0] if args else ""))
            return subprocess.CompletedProcess(
                args="", returncode=0, stdout="", stderr=""
            )

        ok = _set_needs_producer_review(tracking_shell, "WL-1", value=True)
        assert ok is True
        assert "--needs-producer-review true" in shell_calls[0]

    def test_shell_failure(self) -> None:
        def failing_shell(*args, **kwargs):
            return subprocess.CompletedProcess(
                args="", returncode=1, stdout="", stderr="error"
            )

        ok = _set_needs_producer_review(failing_shell, "WL-1")
        assert ok is False
