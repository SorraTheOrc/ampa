"""Integration tests: Scheduler + Engine delegation path.

Tests that the scheduler correctly routes delegation through the engine.
The engine is a hard dependency — there is no legacy fallback path.
"""

import datetime as dt
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.scheduler_types import (
    CommandSpec,
    RunResult,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.delegation import DelegationOrchestrator

from ampa.engine.core import (
    Engine,
    EngineConfig,
    EngineResult,
    EngineStatus,
)
from ampa.engine.candidates import (
    CandidateResult,
    CandidateRejection,
    WorkItemCandidate,
)
from ampa.engine_factory import build_engine as build_engine_factory
from ampa.engine.dispatch import DispatchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    """In-memory store for testing."""

    def __init__(self):
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}
        self._dispatches: List[Dict[str, Any]] = []

    def save(self):
        return None

    def append_dispatch(self, record: dict, retain_last: int = 100) -> str:
        self._dispatches.append(record)
        return f"dispatch-{len(self._dispatches)}"


def _make_config() -> SchedulerConfig:
    return SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )


def _make_spec(
    command_type: str = "delegation",
    audit_only: bool = False,
) -> CommandSpec:
    return CommandSpec(
        command_id="test-delegate",
        command="echo delegate",
        requires_llm=False,
        frequency_minutes=10,
        priority=0,
        metadata={"audit_only": audit_only},
        title="Test Delegation",
        command_type=command_type,
    )


def _noop_executor(spec: CommandSpec) -> RunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return RunResult(start_ts=start, end_ts=end, exit_code=0)


def _noop_run_shell(cmd, **kwargs):
    """Stub run_shell that returns empty success for any command."""
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")


def _make_scheduler(
    engine: Optional[Any] = None,
    run_shell=None,
) -> Scheduler:
    """Construct a Scheduler with an optional engine, suppressing auto-build."""
    store = DummyStore()
    config = _make_config()

    # Suppress auto-construction of engine so we can inject our own
    with mock.patch("ampa.scheduler.build_engine", return_value=(None, None)):
        scheduler = Scheduler(
            store=store,
            config=config,
            executor=_noop_executor,
            run_shell=run_shell or _noop_run_shell,
        )

    # Inject engine after construction and sync to orchestrator
    scheduler.engine = engine
    scheduler._delegation_orchestrator.engine = engine
    return scheduler


def _make_engine_result(
    status: str = EngineStatus.SUCCESS,
    reason: str = "",
    work_item_id: str = "WL-42",
    action: str = "intake",
    dispatch_pid: int = 12345,
    with_candidate: bool = True,
) -> EngineResult:
    """Build an EngineResult with common defaults."""
    dispatch_result = None
    candidate_result = None

    if status == EngineStatus.SUCCESS:
        dispatch_result = DispatchResult(
            success=True,
            command='opencode run "/intake WL-42 do not ask questions"',
            work_item_id=work_item_id,
            timestamp=dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
            pid=dispatch_pid,
        )

    if with_candidate:
        selected = WorkItemCandidate(
            id=work_item_id,
            title="Test Work Item",
            stage="idea",
            status="open",
        )
        candidate_result = CandidateResult(
            selected=selected,
            candidates=(selected,),
        )

    return EngineResult(
        status=status,
        reason=reason,
        work_item_id=work_item_id,
        command_name="delegate",
        action=action,
        dispatch_result=dispatch_result,
        candidate_result=candidate_result,
        timestamp=dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests: run_idle_delegation routes to engine
# ---------------------------------------------------------------------------


class TestEngineRouting:
    """Tests that run_idle_delegation routes to the engine."""

    def test_routes_to_engine_when_available(self):
        """When engine is set, delegation uses engine."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = _make_engine_result(
            status=EngineStatus.SUCCESS,
        )

        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        engine.process_delegation.assert_called_once()
        assert result["dispatched"] is True
        assert "intake" in result["note"]
        assert result["delegate_info"]["id"] == "WL-42"
        assert result["delegate_info"]["action"] == "intake"
        assert result["delegate_info"]["pid"] == 12345

    def test_engine_none_raises_assertion(self):
        """When engine is None, run_idle_delegation raises AssertionError."""
        scheduler = _make_scheduler(engine=None)

        with pytest.raises(AssertionError):
            scheduler._delegation_orchestrator.run_idle_delegation(
                audit_only=False, spec=_make_spec()
            )

    def test_audit_only_skips_engine(self):
        """audit_only=True returns early without calling the engine."""
        engine = mock.MagicMock(spec=Engine)
        scheduler = _make_scheduler(engine=engine)

        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=True, spec=_make_spec()
        )

        engine.process_delegation.assert_not_called()
        assert result["dispatched"] is False
        assert "audit_only" in result["note"]


# ---------------------------------------------------------------------------
# Tests: EngineResult → legacy dict conversion
# ---------------------------------------------------------------------------


class TestEngineResultConversion:
    """Tests the conversion from EngineResult to the legacy dict format."""

    def test_success_result(self):
        """SUCCESS maps to dispatched=True with delegate_info."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = _make_engine_result(
            status=EngineStatus.SUCCESS,
            action="plan",
            work_item_id="WL-99",
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is True
        assert result["note"] == "Delegation: dispatched plan WL-99"
        assert result["delegate_info"]["action"] == "plan"
        assert result["delegate_info"]["id"] == "WL-99"

    def test_success_propagates_container_id_none(self):
        """When dispatch has no container_id, delegate_info carries None."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = _make_engine_result(
            status=EngineStatus.SUCCESS,
            action="intake",
            work_item_id="WL-CID-NONE",
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is True
        assert result["delegate_info"]["container_id"] is None

    def test_success_propagates_container_id_present(self):
        """When dispatch has a container_id, delegate_info carries it."""
        dr = DispatchResult(
            success=True,
            command='opencode run "/intake WL-CID-SET"',
            work_item_id="WL-CID-SET",
            timestamp=dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
            pid=54321,
            container_id="podman-abc123",
        )
        selected = WorkItemCandidate(
            id="WL-CID-SET",
            title="Container Test Item",
            stage="idea",
            status="open",
        )
        candidate_result = CandidateResult(
            selected=selected,
            candidates=(selected,),
        )
        er = EngineResult(
            status=EngineStatus.SUCCESS,
            reason="",
            work_item_id="WL-CID-SET",
            command_name="delegate",
            action="intake",
            dispatch_result=dr,
            candidate_result=candidate_result,
            timestamp=dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
        )
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = er
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is True
        assert result["delegate_info"]["container_id"] == "podman-abc123"
        assert result["delegate_info"]["pid"] == 54321

    def test_no_candidates_result(self):
        """NO_CANDIDATES maps to dispatched=False, idle_notification_sent=True."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = _make_engine_result(
            status=EngineStatus.NO_CANDIDATES,
            reason="No candidates available.",
            with_candidate=False,
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert result["idle_notification_sent"] is True
        assert "no wl next candidates" in result["note"]

    def test_skipped_result(self):
        """SKIPPED maps to dispatched=False with reason in note."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = EngineResult(
            status=EngineStatus.SKIPPED,
            reason="Fallback mode is 'hold'",
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "hold" in result["note"]

    def test_rejected_result(self):
        """REJECTED maps to dispatched=False with rejected list."""
        rejected_candidate = WorkItemCandidate(
            id="WL-50",
            title="Wrong Stage Item",
            stage="done",
            status="closed",
        )
        rejection = CandidateRejection(
            candidate=rejected_candidate,
            reason="unsupported stage 'done'",
        )
        cr = CandidateResult(
            selected=None,
            candidates=(rejected_candidate,),
            rejections=(rejection,),
        )
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = EngineResult(
            status=EngineStatus.REJECTED,
            reason="from-state mismatch",
            work_item_id="WL-50",
            candidate_result=cr,
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "blocked" in result["note"]
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["id"] == "WL-50"
        assert result["rejected"][0]["reason"] == "unsupported stage 'done'"

    def test_invariant_failed_result(self):
        """INVARIANT_FAILED maps to dispatched=False."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = EngineResult(
            status=EngineStatus.INVARIANT_FAILED,
            reason="Pre-invariant check failed: has_title failed",
            work_item_id="WL-60",
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "blocked" in result["note"]
        assert (
            "invariant" in result["note"].lower() or "blocked" in result["note"].lower()
        )

    def test_dispatch_failed_result(self):
        """DISPATCH_FAILED maps to dispatched=False with error."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = EngineResult(
            status=EngineStatus.DISPATCH_FAILED,
            reason="Dispatch failed: process spawn error",
            work_item_id="WL-70",
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "failed" in result["note"]
        assert "error" in result

    def test_error_result(self):
        """ERROR maps to dispatched=False with error details."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.return_value = EngineResult(
            status=EngineStatus.ERROR,
            reason="No 'delegate' command defined in workflow descriptor",
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "engine error" in result["note"]
        assert "error" in result

    def test_engine_exception_caught(self):
        """If engine.process_delegation raises, return graceful error dict."""
        engine = mock.MagicMock(spec=Engine)
        engine.process_delegation.side_effect = RuntimeError("boom")
        scheduler = _make_scheduler(engine=engine)
        result = scheduler._delegation_orchestrator.run_idle_delegation(
            audit_only=False, spec=_make_spec()
        )

        assert result["dispatched"] is False
        assert "engine error" in result["note"]
        assert result["error"] == "engine exception"


# ---------------------------------------------------------------------------
# Tests: _engine_rejections helper
# ---------------------------------------------------------------------------


class TestEngineRejections:
    """Tests the DelegationOrchestrator._engine_rejections static method."""

    def test_no_candidate_result(self):
        """Returns empty list when candidate_result is None."""
        result = EngineResult(
            status=EngineStatus.ERROR,
            reason="test",
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        assert DelegationOrchestrator._engine_rejections(result) == []

    def test_no_rejections(self):
        """Returns empty list when there are no rejections."""
        cr = CandidateResult(selected=None, candidates=())
        result = EngineResult(
            status=EngineStatus.NO_CANDIDATES,
            reason="test",
            candidate_result=cr,
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        assert DelegationOrchestrator._engine_rejections(result) == []

    def test_with_rejections(self):
        """Returns formatted rejection dicts."""
        c1 = WorkItemCandidate(id="WL-1", title="Item 1", stage="done", status="closed")
        c2 = WorkItemCandidate(id="WL-2", title="Item 2", stage="idea", status="open")
        cr = CandidateResult(
            selected=None,
            candidates=(c1, c2),
            rejections=(
                CandidateRejection(candidate=c1, reason="do-not-delegate"),
                CandidateRejection(candidate=c2, reason="unsupported stage"),
            ),
        )
        result = EngineResult(
            status=EngineStatus.REJECTED,
            reason="test",
            candidate_result=cr,
            timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        rejected = DelegationOrchestrator._engine_rejections(result)
        assert len(rejected) == 2
        assert rejected[0] == {
            "id": "WL-1",
            "title": "Item 1",
            "reason": "do-not-delegate",
        }
        assert rejected[1] == {
            "id": "WL-2",
            "title": "Item 2",
            "reason": "unsupported stage",
        }


# ---------------------------------------------------------------------------
# Tests: build_engine factory
# ---------------------------------------------------------------------------


class TestBuildEngine:
    """Tests the build_engine factory function."""

    def test_build_engine_returns_none_when_descriptor_missing(self):
        """When the descriptor file doesn't exist, build_engine returns (None, None)."""
        scheduler = _make_scheduler(engine=None)

        with mock.patch.dict(
            "os.environ", {"AMPA_WORKFLOW_DESCRIPTOR": "/nonexistent/path.yaml"}
        ):
            result, _selector = build_engine_factory(
                run_shell=scheduler.run_shell,
                command_cwd=scheduler.command_cwd,
                store=scheduler.store,
            )

        assert result is None

    def test_build_engine_returns_engine_with_valid_descriptor(self):
        """When a valid descriptor exists, build_engine returns an Engine."""
        import os

        descriptor_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "docs",
            "workflow",
            "workflow.yaml",
        )
        if not os.path.isfile(descriptor_path):
            pytest.skip("workflow.yaml not found")

        scheduler = _make_scheduler(engine=None)

        with mock.patch.dict(
            "os.environ", {"AMPA_WORKFLOW_DESCRIPTOR": descriptor_path}
        ):
            result, _selector = build_engine_factory(
                run_shell=scheduler.run_shell,
                command_cwd=scheduler.command_cwd,
                store=scheduler.store,
            )

        assert result is not None
        assert isinstance(result, Engine)


# ---------------------------------------------------------------------------
# Tests: Scheduler constructor with engine
# ---------------------------------------------------------------------------


class TestSchedulerEngineInit:
    """Tests that the Scheduler properly handles the engine parameter."""

    def test_explicit_engine_injection(self):
        """Passing engine= to Scheduler sets self.engine."""
        engine = mock.MagicMock(spec=Engine)

        with mock.patch("ampa.scheduler.build_engine", return_value=(None, None)):
            scheduler = Scheduler(
                store=DummyStore(),
                config=_make_config(),
                executor=_noop_executor,
                run_shell=_noop_run_shell,
                engine=engine,
            )

        assert scheduler.engine is engine

    def test_auto_builds_engine_when_descriptor_available(self):
        """Constructor auto-builds engine when workflow descriptor exists."""
        import os

        descriptor_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "docs",
            "workflow",
            "workflow.yaml",
        )
        if not os.path.isfile(descriptor_path):
            pytest.skip("workflow.yaml not found")

        with mock.patch.dict(
            "os.environ", {"AMPA_WORKFLOW_DESCRIPTOR": descriptor_path}
        ):
            scheduler = Scheduler(
                store=DummyStore(),
                config=_make_config(),
                executor=_noop_executor,
                run_shell=_noop_run_shell,
            )

        assert scheduler.engine is not None


# ---------------------------------------------------------------------------
# Tests: Adapter classes
# ---------------------------------------------------------------------------


class TestShellAdapters:
    """Smoke tests for adapter classes in ampa.engine.adapters."""

    def test_shell_candidate_fetcher_success(self):
        from ampa.engine.adapters import ShellCandidateFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='[{"id": "WL-1", "title": "Test"}]',
                stderr="",
            )

        fetcher = ShellCandidateFetcher(run_shell=mock_shell)
        result = fetcher.fetch()
        assert len(result) == 1
        assert result[0]["id"] == "WL-1"

    def test_shell_candidate_fetcher_failure(self):
        from ampa.engine.adapters import ShellCandidateFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="error"
            )

        fetcher = ShellCandidateFetcher(run_shell=mock_shell)
        result = fetcher.fetch()
        assert result == []

    def test_shell_in_progress_querier_success(self):
        from ampa.engine.adapters import ShellInProgressQuerier

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='[{"id": "WL-1"}, {"id": "WL-2"}]',
                stderr="",
            )

        querier = ShellInProgressQuerier(run_shell=mock_shell)
        assert querier.count_in_progress() == 2

    def test_shell_in_progress_querier_empty(self):
        from ampa.engine.adapters import ShellInProgressQuerier

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="[]", stderr=""
            )

        querier = ShellInProgressQuerier(run_shell=mock_shell)
        assert querier.count_in_progress() == 0

    def test_shell_in_progress_querier_failure_returns_negative(self):
        from ampa.engine.adapters import ShellInProgressQuerier

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="error"
            )

        querier = ShellInProgressQuerier(run_shell=mock_shell)
        assert querier.count_in_progress() == -1

    def test_shell_work_item_fetcher_success(self):
        from ampa.engine.adapters import ShellWorkItemFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"id": "WL-1", "title": "Test"}',
                stderr="",
            )

        fetcher = ShellWorkItemFetcher(run_shell=mock_shell)
        result = fetcher.fetch("WL-1")
        assert result is not None
        assert result["id"] == "WL-1"

    def test_shell_work_item_fetcher_failure(self):
        from ampa.engine.adapters import ShellWorkItemFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="not found"
            )

        fetcher = ShellWorkItemFetcher(run_shell=mock_shell)
        assert fetcher.fetch("WL-1") is None

    def test_shell_updater_success(self):
        from ampa.engine.adapters import ShellWorkItemUpdater

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="{}", stderr=""
            )

        updater = ShellWorkItemUpdater(run_shell=mock_shell)
        assert updater.update("WL-1", status="in_progress", stage="idea") is True

    def test_shell_updater_failure(self):
        from ampa.engine.adapters import ShellWorkItemUpdater

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="fail"
            )

        updater = ShellWorkItemUpdater(run_shell=mock_shell)
        assert updater.update("WL-1", status="in_progress") is False

    def test_shell_comment_writer_success(self):
        from ampa.engine.adapters import ShellCommentWriter

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="{}", stderr=""
            )

        writer = ShellCommentWriter(run_shell=mock_shell)
        assert writer.write_comment("WL-1", "test comment") is True

    def test_store_dispatch_recorder_success(self):
        from ampa.engine.adapters import StoreDispatchRecorder

        store = DummyStore()
        recorder = StoreDispatchRecorder(store=store)
        record_id = recorder.record_dispatch({"work_item_id": "WL-1"})
        assert record_id is not None
        assert len(store._dispatches) == 1

    def test_store_dispatch_recorder_failure(self):
        from ampa.engine.adapters import StoreDispatchRecorder

        failing_store = mock.MagicMock()
        failing_store.append_dispatch.side_effect = RuntimeError("boom")
        recorder = StoreDispatchRecorder(store=failing_store)
        assert recorder.record_dispatch({}) is None

    def test_discord_notification_sender_no_bot_token(self):
        from ampa.engine.adapters import DiscordNotificationSender

        with mock.patch("ampa.notifications.notify", return_value=True):
            sender = DiscordNotificationSender()
            # notify() is mocked → succeeds
            assert sender.send("test message") is True

    def test_candidate_fetcher_nested_format(self):
        from ampa.engine.adapters import ShellCandidateFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"workItems": [{"id": "WL-1"}, {"id": "WL-2"}]}',
                stderr="",
            )

        fetcher = ShellCandidateFetcher(run_shell=mock_shell)
        result = fetcher.fetch()
        assert len(result) == 2

    def test_candidate_fetcher_single_format(self):
        from ampa.engine.adapters import ShellCandidateFetcher

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"workItem": {"id": "WL-1"}}',
                stderr="",
            )

        fetcher = ShellCandidateFetcher(run_shell=mock_shell)
        result = fetcher.fetch()
        assert len(result) == 1
        assert result[0]["id"] == "WL-1"

    def test_in_progress_querier_nested_format(self):
        from ampa.engine.adapters import ShellInProgressQuerier

        def mock_shell(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"items": [{"id": "WL-1"}]}',
                stderr="",
            )

        querier = ShellInProgressQuerier(run_shell=mock_shell)
        assert querier.count_in_progress() == 1
