"""Tests for ampa.engine.core — Engine orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from ampa.engine.candidates import (
    CandidateResult,
    CandidateSelector,
    WorkItemCandidate,
)
from ampa.engine.core import (
    Engine,
    EngineConfig,
    EngineResult,
    EngineStatus,
    NullCommentWriter,
    NullDispatchRecorder,
    NullNotificationSender,
    NullUpdater,
    WorkItemCommentWriter,
    WorkItemFetcher,
    WorkItemUpdater,
)
from ampa.engine.descriptor import (
    Command,
    Effects,
    Invariant,
    Metadata,
    Role,
    StateTuple,
    WorkflowDescriptor,
)
from ampa.engine.dispatch import DispatchResult, DryRunDispatcher
from ampa.engine.invariants import InvariantEvaluator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_TIME = datetime(2026, 2, 22, 6, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED_TIME


def _make_descriptor(
    *,
    delegate_from: list[str] | None = None,
    delegate_to: str = "delegated",
    pre_invariants: tuple[str, ...] = (),
    post_invariants: tuple[str, ...] = (),
    effects: Effects | None = None,
    extra_commands: dict[str, Command] | None = None,
    invariants: tuple[Invariant, ...] | None = None,
    dispatch_map: dict[str, str] | None = None,
) -> WorkflowDescriptor:
    """Build a minimal descriptor with a delegate command."""
    states = {
        "ready_for_intake": StateTuple(status="open", stage="idea"),
        "ready_for_plan": StateTuple(status="open", stage="intake_complete"),
        "ready_for_impl": StateTuple(status="open", stage="plan_complete"),
        "delegated": StateTuple(status="in-progress", stage="delegated"),
        "in_review": StateTuple(status="in-progress", stage="in_review"),
        "done": StateTuple(status="completed", stage="done"),
    }

    from_states = delegate_from or [
        "ready_for_intake",
        "ready_for_plan",
        "ready_for_impl",
    ]

    if dispatch_map is None:
        dispatch_map = {
            "ready_for_intake": 'opencode run "/intake {id} do not ask questions"',
            "ready_for_plan": 'opencode run "/plan {id}"',
            "ready_for_impl": 'opencode run "work on {id} using the implement skill"',
        }

    commands = {
        "delegate": Command(
            name="delegate",
            description="Delegate work",
            from_states=tuple(from_states),
            to=delegate_to,
            actor="PM",
            pre=pre_invariants,
            post=post_invariants,
            effects=effects,
            dispatch_map=dispatch_map,
        ),
    }

    if extra_commands:
        commands.update(extra_commands)

    if invariants is None:
        invariants = (
            Invariant(
                name="requires_acceptance_criteria",
                description="Has AC",
                when=("pre",),
                logic='regex(description, "- \\\\[[ x]\\\\]")',
            ),
            Invariant(
                name="not_do_not_delegate",
                description="Not DND",
                when=("pre",),
                logic='"do-not-delegate" not in tags',
            ),
            Invariant(
                name="requires_approvals",
                description="Has approvals",
                when=("post",),
                logic='regex(comments_text, "approved")',
            ),
        )

    return WorkflowDescriptor(
        version="1.0.0",
        metadata=Metadata(
            name="test-workflow",
            description="Test",
            owner="test",
            roles=(Role(name="PM"), Role(name="Patch", type="agent")),
        ),
        statuses=("open", "in-progress", "completed"),
        stages=(
            "idea",
            "intake_complete",
            "plan_complete",
            "delegated",
            "in_review",
            "done",
        ),
        states=states,
        invariants=invariants,
        commands=commands,
    )


class MockFetcher:
    """Mock WorkItemFetcher that returns preset data."""

    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data
        self.calls: list[str] = []

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        self.calls.append(work_item_id)
        return self.data


class MockUpdater:
    """Mock WorkItemUpdater that records calls."""

    def __init__(self, *, succeed: bool = True, fail_first: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed
        self._fail_first = fail_first
        self._call_count = 0

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        self._call_count += 1
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "status": status,
                "stage": stage,
                "assignee": assignee,
            }
        )
        if self._fail_first and self._call_count == 1:
            return False
        return self._succeed


class MockCommentWriter:
    """Mock comment writer that records calls."""

    def __init__(self):
        self.comments: list[dict[str, str]] = []

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        self.comments.append(
            {
                "work_item_id": work_item_id,
                "comment": comment,
                "author": author,
            }
        )
        return True


class MockNotifier:
    """Mock notification sender."""

    def __init__(self):
        self.messages: list[dict[str, str]] = []

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        self.messages.append({"message": message, "title": title, "level": level})
        return True


class MockRecorder:
    """Mock dispatch recorder."""

    def __init__(self):
        self.records: list[dict[str, Any]] = []

    def record_dispatch(self, record: dict[str, Any]) -> str | None:
        self.records.append(record)
        return f"DR-{len(self.records)}"


def _make_work_item_data(
    *,
    id: str = "WL-1",
    title: str = "Test item",
    description: str = "Test description\n\n## Acceptance Criteria\n- [ ] Do thing",
    status: str = "open",
    stage: str = "plan_complete",
    tags: list[str] | None = None,
    comments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a mock wl show output."""
    return {
        "workItem": {
            "id": id,
            "title": title,
            "description": description,
            "status": status,
            "stage": stage,
            "tags": tags or [],
            "assignee": "",
            "priority": "medium",
        },
        "comments": comments or [],
        "children": [],
    }


def _make_candidate(
    *,
    id: str = "WL-1",
    title: str = "Test item",
    status: str = "open",
    stage: str = "plan_complete",
    tags: tuple[str, ...] = (),
) -> WorkItemCandidate:
    return WorkItemCandidate(
        id=id,
        title=title,
        status=status,
        stage=stage,
        tags=tags,
    )


def _make_candidate_result(
    selected: WorkItemCandidate | None = None,
    **kwargs,
) -> CandidateResult:
    if selected is None:
        selected = _make_candidate()
    return CandidateResult(
        selected=selected,
        candidates=(selected,),
        **kwargs,
    )


def _build_engine(
    *,
    descriptor: WorkflowDescriptor | None = None,
    work_item_data: dict[str, Any] | None = None,
    candidate_result: CandidateResult | None = None,
    updater: MockUpdater | None = None,
    comment_writer: MockCommentWriter | None = None,
    notifier: MockNotifier | None = None,
    recorder: MockRecorder | None = None,
    dispatcher: DryRunDispatcher | None = None,
    config: EngineConfig | None = None,
) -> tuple[Engine, dict[str, Any]]:
    """Build an Engine with all dependencies mocked."""
    desc = descriptor or _make_descriptor()

    if work_item_data is None:
        work_item_data = _make_work_item_data()

    fetcher = MockFetcher(work_item_data)

    if candidate_result is None:
        candidate_result = _make_candidate_result()

    mock_selector = MagicMock(spec=CandidateSelector)
    mock_selector.select.return_value = candidate_result

    evaluator = InvariantEvaluator(desc.invariants)

    disp = dispatcher or DryRunDispatcher(clock=_fixed_clock)
    upd = updater or MockUpdater()
    cw = comment_writer or MockCommentWriter()
    notif = notifier or MockNotifier()
    rec = recorder or MockRecorder()

    engine = Engine(
        descriptor=desc,
        dispatcher=disp,
        candidate_selector=mock_selector,
        invariant_evaluator=evaluator,
        work_item_fetcher=fetcher,
        updater=upd,
        comment_writer=cw,
        dispatch_recorder=rec,
        notifier=notif,
        config=config or EngineConfig(),
        clock=_fixed_clock,
    )

    deps = {
        "fetcher": fetcher,
        "selector": mock_selector,
        "dispatcher": disp,
        "updater": upd,
        "comment_writer": cw,
        "notifier": notif,
        "recorder": rec,
    }
    return engine, deps


# ---------------------------------------------------------------------------
# Mode A: process_delegation tests
# ---------------------------------------------------------------------------


class TestHappyPathDelegation:
    """Happy path: candidate selected, invariants pass, dispatch succeeds."""

    def test_successful_delegation(self):
        engine, deps = _build_engine()
        result = engine.process_delegation()

        assert result.status == EngineStatus.SUCCESS
        assert result.work_item_id == "WL-1"
        assert result.command_name == "delegate"
        assert result.action == "ready_for_impl"
        assert result.dispatch_result is not None
        assert result.dispatch_result.success is True

    def test_dispatch_recorded(self):
        engine, deps = _build_engine()
        engine.process_delegation()

        rec = deps["recorder"]
        assert len(rec.records) == 1
        assert rec.records[0]["work_item_id"] == "WL-1"
        assert rec.records[0]["action"] == "ready_for_impl"
        assert rec.records[0]["status"] == "dispatched"

    def test_state_transition_applied(self):
        engine, deps = _build_engine()
        engine.process_delegation()

        upd = deps["updater"]
        assert len(upd.calls) == 1
        assert upd.calls[0]["status"] == "in-progress"
        assert upd.calls[0]["stage"] == "delegated"

    def test_post_dispatch_notification(self):
        engine, deps = _build_engine()
        engine.process_delegation()

        notif = deps["notifier"]
        assert len(notif.messages) == 1
        assert "Delegated" in notif.messages[0]["message"]
        assert notif.messages[0]["title"] == "Delegation Dispatch"

    def test_dispatcher_called(self):
        engine, deps = _build_engine()
        engine.process_delegation()

        disp = deps["dispatcher"]
        assert len(disp.calls) == 1
        assert (
            "implement" in disp.calls[0].command.lower()
            or "work on" in disp.calls[0].command
        )

    def test_timestamp(self):
        engine, deps = _build_engine()
        result = engine.process_delegation()
        assert result.timestamp == FIXED_TIME

    def test_summary(self):
        engine, deps = _build_engine()
        result = engine.process_delegation()
        s = result.summary()
        assert "success" in s
        assert "WL-1" in s


class TestStageToActionMapping:
    """Test that different stages resolve to correct dispatch templates."""

    def test_idea_dispatches_intake(self):
        candidate = _make_candidate(stage="idea")
        cr = _make_candidate_result(selected=candidate)
        wi = _make_work_item_data(stage="idea")

        engine, deps = _build_engine(candidate_result=cr, work_item_data=wi)
        result = engine.process_delegation()

        assert result.action == "ready_for_intake"
        assert "/intake" in deps["dispatcher"].calls[0].command

    def test_intake_complete_dispatches_plan(self):
        candidate = _make_candidate(stage="intake_complete")
        cr = _make_candidate_result(selected=candidate)
        wi = _make_work_item_data(stage="intake_complete")

        engine, deps = _build_engine(candidate_result=cr, work_item_data=wi)
        result = engine.process_delegation()

        assert result.action == "ready_for_plan"
        assert "/plan" in deps["dispatcher"].calls[0].command

    def test_plan_complete_dispatches_implement(self):
        candidate = _make_candidate(stage="plan_complete")
        cr = _make_candidate_result(selected=candidate)
        wi = _make_work_item_data(stage="plan_complete")

        engine, deps = _build_engine(candidate_result=cr, work_item_data=wi)
        result = engine.process_delegation()

        assert result.action == "ready_for_impl"
        assert "implement" in deps["dispatcher"].calls[0].command


class TestNoCandidates:
    """Test when no candidates are available."""

    def test_no_candidates_result(self):
        cr = CandidateResult(selected=None)
        engine, deps = _build_engine(candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.NO_CANDIDATES
        assert result.candidate_result is not None

    def test_no_candidates_notification(self):
        cr = CandidateResult(selected=None)
        engine, deps = _build_engine(candidate_result=cr)
        engine.process_delegation()

        notif = deps["notifier"]
        assert len(notif.messages) == 1
        assert "idle" in notif.messages[0]["message"].lower()


class TestInvalidFromState:
    """Test when work item is in an invalid from-state."""

    def test_wrong_stage_rejected(self):
        candidate = _make_candidate(stage="in_review", status="in-progress")
        cr = _make_candidate_result(selected=candidate)
        wi = _make_work_item_data(stage="in_review", status="in-progress")

        engine, deps = _build_engine(candidate_result=cr, work_item_data=wi)
        result = engine.process_delegation()

        assert result.status == EngineStatus.REJECTED
        assert "not a valid from-state" in result.reason

    def test_wrong_status_rejected(self):
        candidate = _make_candidate(stage="plan_complete", status="blocked")
        cr = _make_candidate_result(selected=candidate)
        wi = _make_work_item_data(stage="plan_complete", status="blocked")

        engine, deps = _build_engine(candidate_result=cr, work_item_data=wi)
        result = engine.process_delegation()

        assert result.status == EngineStatus.REJECTED


class TestPreInvariantFailure:
    """Test when pre-invariants fail."""

    def test_pre_invariant_blocks_delegation(self):
        desc = _make_descriptor(
            pre_invariants=("requires_acceptance_criteria",),
        )
        wi = _make_work_item_data(description="No acceptance criteria here")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert result.invariant_result is not None
        assert not result.invariant_result.passed

    def test_pre_invariant_failure_records_comment(self):
        desc = _make_descriptor(
            pre_invariants=("requires_acceptance_criteria",),
        )
        wi = _make_work_item_data(description="No AC")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        engine.process_delegation()

        cw = deps["comment_writer"]
        assert len(cw.comments) == 1
        assert "blocked" in cw.comments[0]["comment"].lower()

    def test_pre_invariant_failure_sends_notification(self):
        desc = _make_descriptor(
            pre_invariants=("requires_acceptance_criteria",),
        )
        wi = _make_work_item_data(description="No AC")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        engine.process_delegation()

        notif = deps["notifier"]
        assert len(notif.messages) == 1
        assert notif.messages[0]["level"] == "warning"

    def test_no_state_transition_on_invariant_failure(self):
        desc = _make_descriptor(
            pre_invariants=("requires_acceptance_criteria",),
        )
        wi = _make_work_item_data(description="No AC")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        engine.process_delegation()

        upd = deps["updater"]
        assert len(upd.calls) == 0

    def test_no_dispatch_on_invariant_failure(self):
        desc = _make_descriptor(
            pre_invariants=("requires_acceptance_criteria",),
        )
        wi = _make_work_item_data(description="No AC")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        engine.process_delegation()

        disp = deps["dispatcher"]
        assert len(disp.calls) == 0

    def test_do_not_delegate_tag_blocks(self):
        desc = _make_descriptor(
            pre_invariants=("not_do_not_delegate",),
        )
        wi = _make_work_item_data(tags=["do-not-delegate"])

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED


class TestDispatchFailure:
    """Test when dispatch fails."""

    def test_dispatch_failure_result(self):
        disp = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-1"})
        engine, deps = _build_engine(dispatcher=disp)
        result = engine.process_delegation()

        assert result.status == EngineStatus.DISPATCH_FAILED
        assert result.dispatch_result is not None
        assert not result.dispatch_result.success

    def test_dispatch_failure_notification(self):
        disp = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-1"})
        notif = MockNotifier()
        engine, deps = _build_engine(dispatcher=disp, notifier=notif)
        engine.process_delegation()

        assert any("failed" in m["message"].lower() for m in notif.messages)

    def test_state_still_transitioned_before_dispatch(self):
        """State is applied in step 3 before dispatch in step 4."""
        disp = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-1"})
        upd = MockUpdater()
        engine, deps = _build_engine(dispatcher=disp, updater=upd)
        engine.process_delegation()

        # State transition was applied even though dispatch failed
        assert len(upd.calls) == 1


class TestUpdateFailure:
    """Test when wl update fails."""

    def test_update_failure_after_retry(self):
        upd = MockUpdater(succeed=False)
        engine, deps = _build_engine(updater=upd)
        result = engine.process_delegation()

        assert result.status == EngineStatus.UPDATE_FAILED
        # Should have retried once (2 total calls)
        assert len(upd.calls) == 2

    def test_update_succeeds_on_retry(self):
        upd = MockUpdater(fail_first=True)
        engine, deps = _build_engine(updater=upd)
        result = engine.process_delegation()

        assert result.status == EngineStatus.SUCCESS
        assert len(upd.calls) == 2  # First failed, second succeeded


class TestFallbackModes:
    """Test fallback mode handling."""

    def test_hold_mode_skips(self):
        config = EngineConfig(fallback_mode="hold")
        engine, deps = _build_engine(config=config)
        result = engine.process_delegation()

        assert result.status == EngineStatus.SKIPPED
        assert "hold" in result.reason

    def test_audit_only_skips(self):
        config = EngineConfig(audit_only=True)
        engine, deps = _build_engine(config=config)
        result = engine.process_delegation()

        assert result.status == EngineStatus.SKIPPED
        assert "audit_only" in result.reason

    def test_auto_decline_overrides_action(self):
        config = EngineConfig(fallback_mode="auto-decline")
        # auto-decline skips dispatch entirely and returns SKIPPED
        engine, deps = _build_engine(config=config)
        result = engine.process_delegation()

        assert result.status == EngineStatus.SKIPPED
        assert "auto-decline" in result.reason

    def test_auto_accept_overrides_action(self):
        config = EngineConfig(fallback_mode="auto-accept")
        engine, deps = _build_engine(config=config)
        result = engine.process_delegation()

        # auto-accept proceeds normally using the from-state alias
        # for dispatch template lookup (not "accept" as a template key)
        assert result.status == EngineStatus.SUCCESS


class TestSpecificWorkItemId:
    """Test when a specific work_item_id is provided."""

    def test_bypasses_candidate_selection(self):
        engine, deps = _build_engine()
        result = engine.process_delegation(work_item_id="WL-1")

        # Selector should not be called
        deps["selector"].select.assert_not_called()
        assert result.status == EngineStatus.SUCCESS

    def test_fetches_work_item_state(self):
        engine, deps = _build_engine()
        engine.process_delegation(work_item_id="WL-1")

        fetcher = deps["fetcher"]
        assert "WL-1" in fetcher.calls

    def test_returns_error_if_fetch_fails(self):
        engine, deps = _build_engine(work_item_data=None)
        # Override fetcher to return None
        deps["fetcher"].data = None
        result = engine.process_delegation(work_item_id="WL-MISSING")

        assert result.status == EngineStatus.ERROR


class TestEffectsAssignee:
    """Test that effects.set_assignee is applied during state transition."""

    def test_assignee_set_from_effects(self):
        effects = Effects(set_assignee="agent-patch")
        desc = _make_descriptor(effects=effects)

        engine, deps = _build_engine(descriptor=desc)
        engine.process_delegation()

        upd = deps["updater"]
        assert upd.calls[0]["assignee"] == "agent-patch"

    def test_no_assignee_without_effects(self):
        engine, deps = _build_engine()
        engine.process_delegation()

        upd = deps["updater"]
        assert upd.calls[0]["assignee"] is None


# ---------------------------------------------------------------------------
# Mode B: process_transition tests
# ---------------------------------------------------------------------------


class TestTransitionHappyPath:
    """Happy path for agent callback transitions."""

    def test_successful_transition(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(status="in-progress", stage="delegated")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        result = engine.process_transition("WL-1", target_stage="in_review")

        assert result.status == EngineStatus.SUCCESS
        assert result.command_name == "complete_work"

    def test_transition_updates_state(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(status="in-progress", stage="delegated")

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        engine.process_transition("WL-1", target_stage="in_review")

        upd = deps["updater"]
        assert len(upd.calls) == 1
        assert upd.calls[0]["status"] == "in-progress"
        assert upd.calls[0]["stage"] == "in_review"


class TestTransitionPostInvariants:
    """Test post-invariant evaluation on transitions."""

    def test_post_invariant_pass(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
            post=("requires_approvals",),
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(
            status="in-progress",
            stage="delegated",
            comments=[{"comment": "Work approved by producer"}],
        )

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        result = engine.process_transition("WL-1", target_stage="in_review")

        assert result.status == EngineStatus.SUCCESS

    def test_post_invariant_fail(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
            post=("requires_approvals",),
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(
            status="in-progress",
            stage="delegated",
            comments=[{"comment": "Some random comment"}],
        )

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi)
        result = engine.process_transition("WL-1", target_stage="in_review")

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert result.invariant_result is not None

    def test_post_invariant_failure_records_comment(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
            post=("requires_approvals",),
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(
            status="in-progress",
            stage="delegated",
        )

        cw = MockCommentWriter()
        engine, deps = _build_engine(
            descriptor=desc, work_item_data=wi, comment_writer=cw
        )
        engine.process_transition("WL-1", target_stage="in_review")

        assert len(cw.comments) == 1
        assert "refused" in cw.comments[0]["comment"].lower()

    def test_no_update_on_post_invariant_failure(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
            post=("requires_approvals",),
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(status="in-progress", stage="delegated")
        upd = MockUpdater()

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi, updater=upd)
        engine.process_transition("WL-1", target_stage="in_review")

        assert len(upd.calls) == 0


class TestTransitionNoCommand:
    """Test when no command matches the transition."""

    def test_no_matching_command(self):
        wi = _make_work_item_data(status="in-progress", stage="delegated")

        engine, deps = _build_engine(work_item_data=wi)
        result = engine.process_transition("WL-1", target_stage="done")

        assert result.status == EngineStatus.REJECTED
        assert "No command found" in result.reason


class TestTransitionFetchFailure:
    """Test when fetching work item data fails for transition."""

    def test_fetch_failure(self):
        engine, deps = _build_engine(work_item_data=None)
        deps["fetcher"].data = None
        result = engine.process_transition("WL-MISSING", target_stage="in_review")

        assert result.status == EngineStatus.ERROR


class TestTransitionUpdateFailure:
    """Test when wl update fails during transition."""

    def test_update_failure_retries(self):
        complete_cmd = Command(
            name="complete_work",
            description="Complete work",
            from_states=("delegated",),
            to="in_review",
            actor="Patch",
        )
        desc = _make_descriptor(extra_commands={"complete_work": complete_cmd})
        wi = _make_work_item_data(status="in-progress", stage="delegated")
        upd = MockUpdater(succeed=False)

        engine, deps = _build_engine(descriptor=desc, work_item_data=wi, updater=upd)
        result = engine.process_transition("WL-1", target_stage="in_review")

        assert result.status == EngineStatus.UPDATE_FAILED
        assert len(upd.calls) == 2  # retried once


# ---------------------------------------------------------------------------
# EngineResult tests
# ---------------------------------------------------------------------------


class TestEngineResult:
    """Tests for EngineResult."""

    def test_success_property(self):
        r = EngineResult(status=EngineStatus.SUCCESS)
        assert r.success is True

    def test_failure_property(self):
        r = EngineResult(status=EngineStatus.INVARIANT_FAILED)
        assert r.success is False

    def test_summary_with_all_fields(self):
        r = EngineResult(
            status=EngineStatus.SUCCESS,
            work_item_id="WL-1",
            action="implement",
            reason="All good",
        )
        s = r.summary()
        assert "success" in s
        assert "WL-1" in s
        assert "implement" in s
        assert "All good" in s


class TestEngineConfig:
    """Tests for EngineConfig defaults."""

    def test_defaults(self):
        c = EngineConfig()
        assert c.max_concurrency == 1
        assert c.fallback_mode is None
        assert c.audit_only is False
        assert c.descriptor_path == ""


# ---------------------------------------------------------------------------
# Null implementations
# ---------------------------------------------------------------------------


class TestNullImplementations:
    """Verify no-op defaults work correctly."""

    def test_null_updater(self):
        u = NullUpdater()
        assert u.update("WL-1", status="open") is True

    def test_null_comment_writer(self):
        w = NullCommentWriter()
        assert w.write_comment("WL-1", "test") is True

    def test_null_recorder(self):
        r = NullDispatchRecorder()
        assert r.record_dispatch({}) is None

    def test_null_notifier(self):
        n = NullNotificationSender()
        assert n.send("test") is True
