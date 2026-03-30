"""Engine integration tests — state transitions, invariant enforcement,
delegation lifecycle, and edge cases.

Covers test plan categories T-ST, T-IE, T-DL, and T-EC from
``docs/workflow/test-plan.md``.  Each test is tagged with its test plan ID
in the docstring.

These tests exercise the ``Engine`` orchestrator (``ampa.engine.core``) with
fully wired-up descriptors matching the canonical ``workflow.yaml`` semantics,
using mock infrastructure (fetcher, updater, dispatcher, notifier).

Design constraints from the revised PRD (docs/workflow/engine-prd.md):

- Post-invariant failures **refuse** the transition (no rollback — the state
  was never advanced).
- Pre-invariant failures **block dispatch** and trigger Discord notification.
- State transition is applied **before** agent dispatch.
- ``requires_work_item_context`` is a PM quality gate using audit skill
  prompts, not raw character count (but the invariant logic currently uses
  ``length(description) > 100`` as a proxy — tests use that logic).
- Only **post-dispatch** notification is sent (no pre-dispatch).
- Engine is a **one-off command** — processes a single work item per
  invocation.
"""

from __future__ import annotations

import time
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
)
from ampa.engine.descriptor import (
    Command,
    Effects,
    InputField,
    Invariant,
    Metadata,
    Notification,
    Role,
    StateTuple,
    WorkflowDescriptor,
)
from ampa.engine.dispatch import DryRunDispatcher
from ampa.engine.invariants import InvariantEvaluator, NullQuerier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXED_TIME = datetime(2026, 2, 24, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED_TIME


# ---------------------------------------------------------------------------
# Mock infrastructure (mirrors test_core.py patterns)
# ---------------------------------------------------------------------------


class MockFetcher:
    """Mock WorkItemFetcher that returns preset data, keyed by work_item_id."""

    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data
        self.calls: list[str] = []

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        self.calls.append(work_item_id)
        return self.data


class MockUpdater:
    """Mock WorkItemUpdater that records calls."""

    def __init__(self, *, succeed: bool = True):
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
    """Mock comment writer that records calls."""

    def __init__(self):
        self.comments: list[dict[str, str]] = []

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        self.comments.append(
            {"work_item_id": work_item_id, "comment": comment, "author": author}
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


class MockQuerier:
    """Mock WorkItemQuerier with configurable in-progress count."""

    def __init__(self, count: int = 0):
        self._count = count

    def count_in_progress(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# Descriptor builder — builds the full canonical workflow state machine
# ---------------------------------------------------------------------------

# Full state map matching workflow.yaml
STATES = {
    "idea": StateTuple(status="open", stage="idea"),
    "intake": StateTuple(status="open", stage="intake_complete"),
    "prd": StateTuple(status="open", stage="prd_complete"),
    "plan": StateTuple(status="open", stage="plan_complete"),
    "building": StateTuple(status="in_progress", stage="in_progress"),
    "review": StateTuple(status="in_progress", stage="in_review"),
    "shipped": StateTuple(status="closed", stage="done"),
    "delegated": StateTuple(status="in-progress", stage="in_progress"),
    "audit_passed": StateTuple(status="completed", stage="audit_passed"),
    "audit_failed": StateTuple(status="in_progress", stage="audit_failed"),
    "escalated": StateTuple(status="blocked", stage="escalated"),
    "blocked_in_progress": StateTuple(status="blocked", stage="in_progress"),
    "blocked_delegated": StateTuple(status="blocked", stage="in_progress"),
}

STATUSES = ("open", "in_progress", "in-progress", "blocked", "completed", "closed")
STAGES = (
    "idea",
    "intake_complete",
    "prd_complete",
    "plan_complete",
    "in_progress",
    "in_review",
    "audit_passed",
    "audit_failed",
    "escalated",
    "done",
)

# Full invariant set matching workflow.yaml
INVARIANTS = (
    Invariant(
        name="requires_prd_link",
        description="PRD URL must be present",
        when=("pre",),
        logic='regex(description, "PRD:\\\\s*https?://")',
    ),
    Invariant(
        name="requires_tests",
        description="Test plan link must exist",
        when=("pre",),
        logic='regex(description, "Test Plan:\\\\s*https?://")',
    ),
    Invariant(
        name="requires_approvals",
        description="At least one reviewer approved",
        when=("post",),
        logic='regex(comments, "Approved by\\\\s+\\\\w+")',
    ),
    Invariant(
        name="requires_work_item_context",
        description="Work item has sufficient context",
        when=("pre",),
        logic="length(description) > 100",
    ),
    Invariant(
        name="requires_acceptance_criteria",
        description="Has acceptance criteria",
        when=("pre",),
        logic='regex(description, "(?i)(acceptance criteria|\\\\- \\\\[[ x]\\\\])")',
    ),
    Invariant(
        name="requires_stage_for_delegation",
        description="Stage must be idea/intake_complete/plan_complete",
        when=("pre",),
        logic='stage in ["idea", "intake_complete", "plan_complete"]',
    ),
    Invariant(
        name="not_do_not_delegate",
        description="Not tagged do-not-delegate",
        when=("pre",),
        logic='"do-not-delegate" not in tags',
    ),
    Invariant(
        name="no_in_progress_items",
        description="No other items in progress",
        when=("pre",),
        logic='count(work_items, status="in_progress") == 0',
    ),
    Invariant(
        name="requires_audit_result",
        description="Audit comment must exist",
        when=("pre",),
        logic='regex(comments, "(?i)AMPA Audit Result")',
    ),
    Invariant(
        name="audit_recommends_closure",
        description="Audit recommends closure",
        when=("pre",),
        logic='regex(comments, "(?i)(can this item be closed\\\\?\\\\s*yes|all acceptance criteria.*(met|satisfied))")',
    ),
    Invariant(
        name="audit_does_not_recommend_closure",
        description="Audit does not recommend closure",
        when=("pre",),
        logic='regex(comments, "(?i)can this item be closed\\\\?\\\\s*no")',
    ),
)

ROLES = (
    Role(name="Producer", description="Human stakeholder", type="human"),
    Role(name="PM", description="Product manager", type="either"),
    Role(name="Patch", description="Developer agent", type="agent"),
    Role(name="QA", description="Quality assurance", type="agent"),
    Role(name="DevOps", description="CI/CD", type="either"),
    Role(name="TechnicalWriter", description="Docs", type="either"),
)

# Full command set matching workflow.yaml
COMMANDS = {
    "intake": Command(
        name="intake",
        description="Capture intake details",
        from_states=("idea",),
        to="intake",
        actor="PM",
    ),
    "author_prd": Command(
        name="author_prd",
        description="Produce PRD draft",
        from_states=("intake",),
        to="prd",
        actor="PM",
        pre=("requires_prd_link",),
    ),
    "plan": Command(
        name="plan",
        description="Decompose into sub-tasks",
        from_states=("intake", "prd"),
        to="plan",
        actor="PM",
    ),
    "start_build": Command(
        name="start_build",
        description="Begin manual implementation",
        from_states=("plan",),
        to="building",
        actor="Patch",
    ),
    "block": Command(
        name="block",
        description="Block building work item",
        from_states=("building",),
        to="blocked_in_progress",
        actor="Patch",
    ),
    "block_delegated": Command(
        name="block_delegated",
        description="Block delegated work item",
        from_states=("delegated",),
        to="blocked_delegated",
        actor="Patch",
    ),
    "unblock": Command(
        name="unblock",
        description="Resume building work",
        from_states=("blocked_in_progress",),
        to="building",
        actor="Patch",
    ),
    "unblock_delegated": Command(
        name="unblock_delegated",
        description="Resume delegated work",
        from_states=("blocked_delegated",),
        to="delegated",
        actor="Patch",
    ),
    "submit_review": Command(
        name="submit_review",
        description="Submit for review",
        from_states=("building",),
        to="review",
        actor="Patch",
        pre=("requires_tests",),
    ),
    "approve": Command(
        name="approve",
        description="Approve and ship",
        from_states=("review", "audit_passed"),
        to="shipped",
        actor="Producer",
        post=("requires_approvals",),
    ),
    "reopen": Command(
        name="reopen",
        description="Reopen from shipped",
        from_states=("shipped",),
        to="plan",
        actor="Producer",
    ),
    "delegate": Command(
        name="delegate",
        description="AMPA delegates work item",
        from_states=("idea", "intake", "plan"),
        to="delegated",
        actor="PM",
        pre=(
            "requires_work_item_context",
            "requires_acceptance_criteria",
            "requires_stage_for_delegation",
            "not_do_not_delegate",
            "no_in_progress_items",
        ),
        dispatch_map={
            "idea": 'opencode run "/intake {id} do not ask questions"',
            "intake": 'opencode run "/plan {id}"',
            "plan": 'opencode run "work on {id} using the implement skill"',
        },
        effects=Effects(
            set_assignee="Patch",
            add_tags=("delegated",),
            notifications=(
                Notification(
                    channel="discord",
                    message="Delegating task for ${title} (${id})",
                ),
            ),
        ),
    ),
    "complete_work": Command(
        name="complete_work",
        description="Patch reports work complete",
        from_states=("delegated",),
        to="building",
        actor="Patch",
    ),
    "audit_result": Command(
        name="audit_result",
        description="AMPA audit result",
        from_states=("review",),
        to="audit_passed",
        actor="QA",
        pre=("requires_audit_result",),
    ),
    "audit_fail": Command(
        name="audit_fail",
        description="Audit found gaps",
        from_states=("review",),
        to="audit_failed",
        actor="QA",
        pre=("requires_audit_result", "audit_does_not_recommend_closure"),
    ),
    "close_with_audit": Command(
        name="close_with_audit",
        description="Close after audit pass",
        from_states=("audit_passed",),
        to=StateTuple(status="completed", stage="in_review"),
        actor="PM",
        pre=("audit_recommends_closure",),
        effects=Effects(
            set_needs_producer_review=True,
            add_tags=("audit_closed",),
        ),
    ),
    "escalate": Command(
        name="escalate",
        description="Escalate to Producer",
        from_states=("audit_failed", "delegated"),
        to="escalated",
        actor="PM",
        effects=Effects(
            set_assignee="Producer",
            add_tags=("escalated",),
        ),
    ),
    "retry_delegation": Command(
        name="retry_delegation",
        description="Re-delegate after audit failure",
        from_states=("audit_failed", "escalated"),
        to="plan",
        actor="PM",
    ),
    "de_escalate": Command(
        name="de_escalate",
        description="Resolve escalation",
        from_states=("escalated",),
        to="plan",
        actor="Producer",
    ),
}


def _make_descriptor(
    *,
    commands: dict[str, Command] | None = None,
    invariants: tuple[Invariant, ...] | None = None,
    states: dict[str, StateTuple] | None = None,
) -> WorkflowDescriptor:
    """Build a full workflow descriptor matching workflow.yaml semantics."""
    return WorkflowDescriptor(
        version="1.0.0",
        metadata=Metadata(
            name="ampa_prd_workflow",
            description="Integration test workflow",
            owner="workflow-team",
            roles=ROLES,
        ),
        statuses=STATUSES,
        stages=STAGES,
        states=states or STATES,
        terminal_states=("shipped",),
        invariants=invariants or INVARIANTS,
        commands=commands or COMMANDS,
    )


def _make_work_item_data(
    *,
    id: str = "WL-1",
    title: str = "Test item",
    description: str = (
        "Test description with sufficient context for autonomous implementation.\n\n"
        "## Acceptance Criteria\n- [ ] First criterion\n- [ ] Second criterion"
    ),
    status: str = "open",
    stage: str = "plan_complete",
    tags: list[str] | None = None,
    comments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build mock ``wl show`` output."""
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
    return WorkItemCandidate(id=id, title=title, status=status, stage=stage, tags=tags)


def _make_candidate_result(
    selected: WorkItemCandidate | None = None,
    **kwargs,
) -> CandidateResult:
    if selected is None:
        selected = _make_candidate()
    return CandidateResult(selected=selected, candidates=(selected,), **kwargs)


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
    querier: MockQuerier | None = None,
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

    q = querier or MockQuerier(0)
    evaluator = InvariantEvaluator(desc.invariants, querier=q)

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
        "querier": q,
    }
    return engine, deps


# =====================================================================
# 7. State Transition Tests (T-ST)
# =====================================================================


class TestStateTransitions:
    """T-ST: Verify command execution sequences through the state machine.

    These tests exercise Mode A (``process_delegation``) and Mode B
    (``process_transition``) to verify that the full lifecycle of
    state transitions works correctly.
    """

    def test_t_st_01_happy_path_full_lifecycle(self):
        """T-ST-01: Happy path — full lifecycle from idea to shipped.

        Verifies the complete command sequence:
        idea -> intake -> prd -> plan -> delegated -> building -> review ->
        audit_passed -> completed/in_review -> shipped

        Reference: docs/workflow/examples/01-happy-path.md
        """
        desc = _make_descriptor()

        # Verify the full transition chain exists and resolves correctly
        transitions = [
            ("intake", "idea", "intake"),
            ("author_prd", "intake", "prd"),
            ("plan", "prd", "plan"),
            ("delegate", "plan", "delegated"),
            ("complete_work", "delegated", "building"),
            ("submit_review", "building", "review"),
            ("audit_result", "review", "audit_passed"),
        ]

        for cmd_name, from_alias, to_alias in transitions:
            cmd = desc.get_command(cmd_name)
            from_state = desc.resolve_alias(from_alias)
            to_state = desc.resolve_state_ref(cmd.to)

            # Verify from-state is in the command's from_states
            valid_from = [desc.resolve_state_ref(ref) for ref in cmd.from_states]
            assert from_state in valid_from, (
                f"Command '{cmd_name}': {from_alias} not in from_states"
            )

            # Verify to-state resolves to the expected alias
            expected_to = desc.resolve_alias(to_alias)
            assert to_state == expected_to, (
                f"Command '{cmd_name}': to={to_state} != expected {expected_to}"
            )

        # close_with_audit has inline to-state
        close_cmd = desc.get_command("close_with_audit")
        close_to = desc.resolve_state_ref(close_cmd.to)
        assert close_to == StateTuple(status="completed", stage="in_review")

        # approve from audit_passed to shipped
        approve_cmd = desc.get_command("approve")
        approve_to = desc.resolve_state_ref(approve_cmd.to)
        assert approve_to == desc.resolve_alias("shipped")
        ap_valid_from = [desc.resolve_state_ref(r) for r in approve_cmd.from_states]
        assert desc.resolve_alias("audit_passed") in ap_valid_from

    def test_t_st_02_audit_failure_and_retry(self):
        """T-ST-02: Audit failure → retry → re-delegate.

        Expected: review -> audit_failed -> plan -> delegated -> building ->
        review -> audit_passed

        Reference: docs/workflow/examples/02-audit-failure.md
        """
        desc = _make_descriptor()

        transitions = [
            ("audit_fail", "review", "audit_failed"),
            ("retry_delegation", "audit_failed", "plan"),
            ("delegate", "plan", "delegated"),
            ("complete_work", "delegated", "building"),
            ("submit_review", "building", "review"),
            ("audit_result", "review", "audit_passed"),
        ]

        for cmd_name, from_alias, to_alias in transitions:
            cmd = desc.get_command(cmd_name)
            from_state = desc.resolve_alias(from_alias)
            to_state = desc.resolve_state_ref(cmd.to)

            valid_from = [desc.resolve_state_ref(ref) for ref in cmd.from_states]
            assert from_state in valid_from, (
                f"Command '{cmd_name}': {from_alias} not in from_states"
            )

            expected_to = desc.resolve_alias(to_alias)
            assert to_state == expected_to, (
                f"Command '{cmd_name}': to={to_state} != expected {expected_to}"
            )

    def test_t_st_03_blocked_and_unblocked(self):
        """T-ST-03: Block and unblock from delegated state.

        Expected: delegated -> blocked_delegated -> delegated

        Reference: docs/workflow/examples/03-blocked-flow.md
        """
        desc = _make_descriptor()

        # Block delegated
        block_cmd = desc.get_command("block_delegated")
        from_state = desc.resolve_alias("delegated")
        valid_from = [desc.resolve_state_ref(r) for r in block_cmd.from_states]
        assert from_state in valid_from
        assert desc.resolve_state_ref(block_cmd.to) == desc.resolve_alias(
            "blocked_delegated"
        )

        # Unblock delegated
        unblock_cmd = desc.get_command("unblock_delegated")
        blocked_state = desc.resolve_alias("blocked_delegated")
        valid_from = [desc.resolve_state_ref(r) for r in unblock_cmd.from_states]
        assert blocked_state in valid_from
        assert desc.resolve_state_ref(unblock_cmd.to) == desc.resolve_alias("delegated")

    def test_t_st_04_escalation_flow(self):
        """T-ST-04: Escalation from audit_failed.

        Expected: audit_failed -> escalated -> plan -> delegated

        Reference: docs/workflow/examples/06-escalation.md
        """
        desc = _make_descriptor()

        transitions = [
            ("escalate", "audit_failed", "escalated"),
            ("de_escalate", "escalated", "plan"),
            ("delegate", "plan", "delegated"),
        ]

        for cmd_name, from_alias, to_alias in transitions:
            cmd = desc.get_command(cmd_name)
            from_state = desc.resolve_alias(from_alias)
            to_state = desc.resolve_state_ref(cmd.to)

            valid_from = [desc.resolve_state_ref(ref) for ref in cmd.from_states]
            assert from_state in valid_from, (
                f"Command '{cmd_name}': {from_alias} not in from_states"
            )

            expected_to = desc.resolve_alias(to_alias)
            assert to_state == expected_to

    def test_t_st_05_manual_build_path(self):
        """T-ST-05: Manual build path (no delegation).

        Expected: plan -> building -> review -> shipped

        This tests the non-AMPA path through the workflow.
        """
        desc = _make_descriptor()

        transitions = [
            ("start_build", "plan", "building"),
            ("submit_review", "building", "review"),
            ("approve", "review", "shipped"),
        ]

        for cmd_name, from_alias, to_alias in transitions:
            cmd = desc.get_command(cmd_name)
            from_state = desc.resolve_alias(from_alias)
            to_state = desc.resolve_state_ref(cmd.to)

            valid_from = [desc.resolve_state_ref(ref) for ref in cmd.from_states]
            assert from_state in valid_from
            expected_to = desc.resolve_alias(to_alias)
            assert to_state == expected_to

    def test_t_st_06_invalid_transition_rejected(self):
        """T-ST-06: Invalid transition — delegate from idea state.

        The delegate command includes 'idea' in its from_states, so
        delegation from idea IS valid per workflow.yaml. This test verifies
        that delegating from a state NOT in from_states (e.g. 'shipped')
        is rejected by the engine.
        """
        wi = _make_work_item_data(status="closed", stage="done")
        candidate = _make_candidate(status="closed", stage="done")
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.REJECTED
        assert "not a valid from-state" in result.reason

    def test_t_st_07_reopen_from_shipped(self):
        """T-ST-07: Reopen from shipped state.

        Expected: shipped -> plan

        Tests transition from terminal state via explicit reopen command.
        """
        desc = _make_descriptor()

        reopen_cmd = desc.get_command("reopen")
        shipped_state = desc.resolve_alias("shipped")
        valid_from = [desc.resolve_state_ref(r) for r in reopen_cmd.from_states]
        assert shipped_state in valid_from

        to_state = desc.resolve_state_ref(reopen_cmd.to)
        assert to_state == desc.resolve_alias("plan")

        # Also verify via process_transition (Mode B)
        wi = _make_work_item_data(status="closed", stage="done")
        engine, deps = _build_engine(work_item_data=wi)

        result = engine.process_transition("WL-1", "plan_complete")
        assert result.status == EngineStatus.SUCCESS
        assert result.command_name == "reopen"


# =====================================================================
# 8. Invariant Enforcement Tests (T-IE)
# =====================================================================


class TestInvariantEnforcement:
    """T-IE: Verify pre/post invariant behavior through the engine.

    Tests exercise the real InvariantEvaluator with the canonical
    invariant definitions to verify that invariant failures produce
    the expected engine behavior.
    """

    def test_t_ie_01_pre_invariant_blocks_delegation(self):
        """T-IE-01: Pre-invariant failure blocks delegate command.

        Setup: Work item description is too short (< 100 chars).
        Expected: requires_work_item_context fails, no state transition,
        error logged, Discord notification sent.
        """
        wi = _make_work_item_data(description="Too short")
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert result.invariant_result is not None
        assert "requires_work_item_context" in result.invariant_result.failed_invariants

        # No state transition should have occurred
        assert len(deps["updater"].calls) == 0

        # Discord notification sent
        assert len(deps["notifier"].messages) == 1
        assert deps["notifier"].messages[0]["level"] == "warning"

    def test_t_ie_02_do_not_delegate_tag(self):
        """T-IE-02: Pre-invariant — do not delegate tag.

        Setup: Work item tagged 'do-not-delegate'.
        Expected: not_do_not_delegate invariant fails.
        """
        wi = _make_work_item_data(tags=["do-not-delegate"])
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert "not_do_not_delegate" in result.invariant_result.failed_invariants
        assert len(deps["updater"].calls) == 0

    def test_t_ie_03_single_concurrency(self):
        """T-IE-03: Pre-invariant — single concurrency.

        Setup: Another work item is in_progress.
        Expected: no_in_progress_items invariant fails.

        Reference: docs/workflow/examples/05-work-in-progress.md
        """
        wi = _make_work_item_data()
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        # Querier reports 1 item in progress
        q = MockQuerier(count=1)
        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr, querier=q)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert "no_in_progress_items" in result.invariant_result.failed_invariants
        assert len(deps["updater"].calls) == 0

    def test_t_ie_04_no_acceptance_criteria(self):
        """T-IE-04: Pre-invariant — no acceptance criteria.

        Setup: Work item description has no AC section or checkbox list.
        Expected: requires_acceptance_criteria fails.
        """
        # Long description but no AC markers.
        # IMPORTANT: avoid the phrase "acceptance criteria" because the regex
        # pattern matches that phrase case-insensitively.
        long_desc = (
            "This is a work item with a very detailed description that exceeds "
            "the minimum length requirement. It discusses many implementation "
            "details and provides context but has no requirements checklist or "
            "verification steps defined anywhere in the description text body."
        )
        wi = _make_work_item_data(description=long_desc)
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert (
            "requires_acceptance_criteria" in result.invariant_result.failed_invariants
        )
        assert len(deps["updater"].calls) == 0

    def test_t_ie_05_audit_recommends_closure(self):
        """T-IE-05: Pre-invariant — audit recommends closure not present.

        Setup: No audit comment with closure recommendation.
        Expected: audit_recommends_closure fails when trying close_with_audit.

        This test verifies the invariant logic directly since close_with_audit
        is not accessible via process_delegation (which only uses delegate).
        """
        desc = _make_descriptor()
        evaluator = InvariantEvaluator(desc.invariants)

        # Work item with no audit comments
        wi = _make_work_item_data(
            status="completed",
            stage="audit_passed",
            comments=[],
        )

        result = evaluator.evaluate(
            ("audit_recommends_closure",),
            wi,
            fail_fast=False,
        )

        assert not result.passed
        assert "audit_recommends_closure" in result.failed_invariants

    def test_t_ie_06_multiple_pre_invariant_failures(self):
        """T-IE-06: Multiple pre-invariant failures reported.

        Setup: Empty description, no AC, another item in progress.
        Expected: All 3+ invariant failures collected and reported.

        The engine uses fail_fast=False so all failures are collected.
        """
        wi = _make_work_item_data(description="")
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        # Multiple conditions that will cause failures
        q = MockQuerier(count=1)
        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr, querier=q)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert result.invariant_result is not None

        # Multiple failures should be reported (not just the first)
        failed = result.invariant_result.failed_invariants
        assert len(failed) >= 2, f"Expected multiple failures, got: {failed}"

        # At least these should fail
        assert "requires_work_item_context" in failed
        assert "requires_acceptance_criteria" in failed

    def test_t_ie_07_post_invariant_refuses_transition(self):
        """T-IE-07: Post-invariant refuses transition (no rollback).

        Command: approve
        State: review (in_progress/in_review)
        Setup: No 'Approved by' comment.
        Expected: Post-invariant requires_approvals fails.
        Engine behavior: Transition REFUSED (state never advanced, no rollback).

        This tests Mode B (process_transition) since post-invariants are
        evaluated on agent callback.
        """
        wi = _make_work_item_data(
            status="in_progress",
            stage="in_review",
            comments=[],  # No "Approved by" comment
        )

        engine, deps = _build_engine(work_item_data=wi)

        # Agent requests transition to "done" stage (approve)
        result = engine.process_transition("WL-1", "done")

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert result.invariant_result is not None
        assert "requires_approvals" in result.invariant_result.failed_invariants

        # No state update should have occurred (refused, not rolled back)
        assert len(deps["updater"].calls) == 0

        # Comment should be written explaining the refusal
        assert len(deps["comment_writer"].comments) == 1
        assert "refused" in deps["comment_writer"].comments[0]["comment"].lower()


# =====================================================================
# 9. Delegation Lifecycle Tests (T-DL)
# =====================================================================


class TestDelegationLifecycle:
    """T-DL: End-to-end delegation flow scenarios.

    Tests exercise the engine's process_delegation (Mode A) and
    process_transition (Mode B) through realistic scenarios.
    """

    def test_t_dl_01_full_happy_path(self):
        """T-DL-01: Full delegation happy path.

        Scenario: AMPA selects item, delegates, work completes, audit passes.
        Expected: delegate dispatches, state transitions to delegated,
        notification sent, dispatch recorded.

        Reference: docs/workflow/examples/01-happy-path.md
        """
        wi = _make_work_item_data(
            description=(
                "Full feature implementation with comprehensive context.\n\n"
                "## Acceptance Criteria\n"
                "- [ ] Feature implemented\n"
                "- [ ] Tests passing\n"
                "- [ ] Documentation updated"
            ),
        )
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr)
        result = engine.process_delegation()

        # Delegation succeeds
        assert result.status == EngineStatus.SUCCESS
        assert result.work_item_id == "WL-1"
        assert result.command_name == "delegate"

        # State transition applied BEFORE dispatch
        assert len(deps["updater"].calls) == 1
        assert deps["updater"].calls[0]["status"] == "in-progress"
        assert deps["updater"].calls[0]["stage"] == "in_progress"

        # Dispatch occurred
        assert len(deps["dispatcher"].calls) == 1
        assert "implement" in deps["dispatcher"].calls[0].command

        # Post-dispatch notification (not pre-dispatch)
        assert len(deps["notifier"].messages) == 1
        assert "Delegated" in deps["notifier"].messages[0]["message"]

        # Dispatch recorded
        assert len(deps["recorder"].records) == 1

    def test_t_dl_02_audit_failure_with_retry(self):
        """T-DL-02: Delegation → audit failure → retry → re-delegate.

        Scenario: Audit finds gaps, engine retries, second attempt.
        This test verifies the re-delegation path via Mode B transitions.

        Reference: docs/workflow/examples/02-audit-failure.md
        """
        desc = _make_descriptor()

        # Step 1: Verify audit_fail command transitions from review to audit_failed
        audit_fail_cmd = desc.get_command("audit_fail")
        review_state = desc.resolve_alias("review")
        valid_from = [desc.resolve_state_ref(r) for r in audit_fail_cmd.from_states]
        assert review_state in valid_from
        assert desc.resolve_state_ref(audit_fail_cmd.to) == desc.resolve_alias(
            "audit_failed"
        )

        # Step 2: Verify retry_delegation from audit_failed back to plan
        retry_cmd = desc.get_command("retry_delegation")
        af_state = desc.resolve_alias("audit_failed")
        valid_from = [desc.resolve_state_ref(r) for r in retry_cmd.from_states]
        assert af_state in valid_from
        assert desc.resolve_state_ref(retry_cmd.to) == desc.resolve_alias("plan")

        # Step 3: Process_transition from audit_failed to plan_complete (retry)
        wi = _make_work_item_data(
            status="in_progress",
            stage="audit_failed",
        )
        engine, deps = _build_engine(work_item_data=wi)
        result = engine.process_transition("WL-1", "plan_complete")

        assert result.status == EngineStatus.SUCCESS
        assert result.command_name == "retry_delegation"

    def test_t_dl_03_escalation(self):
        """T-DL-03: Escalation after audit failure.

        Scenario: Audit fails, escalation to Producer.

        Reference: docs/workflow/examples/06-escalation.md
        """
        desc = _make_descriptor()

        # Escalation from audit_failed
        escalate_cmd = desc.get_command("escalate")
        af_state = desc.resolve_alias("audit_failed")
        valid_from = [desc.resolve_state_ref(r) for r in escalate_cmd.from_states]
        assert af_state in valid_from

        to_state = desc.resolve_state_ref(escalate_cmd.to)
        assert to_state == desc.resolve_alias("escalated")

        # Verify escalation effects: assignee -> Producer, tags -> [escalated]
        assert escalate_cmd.effects is not None
        assert escalate_cmd.effects.set_assignee == "Producer"
        assert "escalated" in escalate_cmd.effects.add_tags

    def test_t_dl_04_blocked_during_implementation(self):
        """T-DL-04: Blocked during implementation.

        Scenario: Patch encounters a blocker during delegated work.
        Expected: delegated -> blocked_delegated -> delegated -> building -> review

        Reference: docs/workflow/examples/03-blocked-flow.md
        """
        desc = _make_descriptor()

        # Block from delegated
        block_cmd = desc.get_command("block_delegated")
        delegated_state = desc.resolve_alias("delegated")
        valid_from = [desc.resolve_state_ref(r) for r in block_cmd.from_states]
        assert delegated_state in valid_from
        assert desc.resolve_state_ref(block_cmd.to) == desc.resolve_alias(
            "blocked_delegated"
        )

        # Process transition: block delegated item
        wi = _make_work_item_data(status="in-progress", stage="in_progress")
        engine, deps = _build_engine(work_item_data=wi)

        # The engine finds the command matching from=(in-progress, in_progress)
        # and target stage in_progress for blocked_delegated
        # But blocked_delegated has stage=in_progress, same as delegated
        # so we test unblock directly

        # Unblock back to delegated
        unblock_cmd = desc.get_command("unblock_delegated")
        blocked_state = desc.resolve_alias("blocked_delegated")
        valid_from = [desc.resolve_state_ref(r) for r in unblock_cmd.from_states]
        assert blocked_state in valid_from
        assert desc.resolve_state_ref(unblock_cmd.to) == desc.resolve_alias("delegated")

    def test_t_dl_05_no_candidates(self):
        """T-DL-05: No candidates available.

        Scenario: Scheduler runs but wl next returns no candidates.
        Expected: No delegation, idle state, Discord notification sent.

        Reference: docs/workflow/examples/04-no-candidates.md
        """
        cr = CandidateResult(selected=None)
        engine, deps = _build_engine(candidate_result=cr)
        result = engine.process_delegation()

        assert result.status == EngineStatus.NO_CANDIDATES
        assert result.candidate_result is not None

        # Discord notification sent
        assert len(deps["notifier"].messages) == 1
        assert "idle" in deps["notifier"].messages[0]["message"].lower()

        # No state transition or dispatch
        assert len(deps["updater"].calls) == 0
        assert len(deps["dispatcher"].calls) == 0

    def test_t_dl_06_concurrent_work_in_progress(self):
        """T-DL-06: Concurrent work in progress blocks delegation.

        Scenario: Scheduler runs but another item is already in progress.
        Expected: no_in_progress_items invariant fails, delegation skipped.

        Reference: docs/workflow/examples/05-work-in-progress.md
        """
        wi = _make_work_item_data()
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        q = MockQuerier(count=1)
        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr, querier=q)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert "no_in_progress_items" in result.invariant_result.failed_invariants

        # No dispatch occurred
        assert len(deps["dispatcher"].calls) == 0

        # Notification sent about invariant failure
        assert len(deps["notifier"].messages) == 1
        assert deps["notifier"].messages[0]["level"] == "warning"


# =====================================================================
# 10. Edge Case Tests (T-EC)
# =====================================================================


class TestEdgeCases:
    """T-EC: Boundary and error scenarios.

    Tests verify correct handling of unusual or edge-case transitions,
    minimal descriptors, and performance.
    """

    def test_t_ec_01_reopen_after_closure(self):
        """T-EC-01: Reopen a shipped work item.

        Scenario: Item is shipped (terminal), then reopened.
        Expected: State transitions to plan, work item can be re-delegated.
        """
        desc = _make_descriptor()

        # Verify reopen exists from shipped
        reopen_cmd = desc.get_command("reopen")
        shipped_state = desc.resolve_alias("shipped")
        valid_from = [desc.resolve_state_ref(r) for r in reopen_cmd.from_states]
        assert shipped_state in valid_from

        to_state = desc.resolve_state_ref(reopen_cmd.to)
        plan_state = desc.resolve_alias("plan")
        assert to_state == plan_state

        # Verify plan is a valid from-state for delegate (re-delegation possible)
        delegate_cmd = desc.get_command("delegate")
        delegate_from = [desc.resolve_state_ref(r) for r in delegate_cmd.from_states]
        assert plan_state in delegate_from

    def test_t_ec_02_block_from_multiple_states(self):
        """T-EC-02: Block command works from both building and delegated states.

        Scenario: block from building, block_delegated from delegated.
        Expected: building -> blocked_in_progress, delegated -> blocked_delegated
        """
        desc = _make_descriptor()

        # Block from building
        block_cmd = desc.get_command("block")
        building_state = desc.resolve_alias("building")
        valid_from = [desc.resolve_state_ref(r) for r in block_cmd.from_states]
        assert building_state in valid_from
        assert desc.resolve_state_ref(block_cmd.to) == desc.resolve_alias(
            "blocked_in_progress"
        )

        # Block from delegated
        block_d_cmd = desc.get_command("block_delegated")
        delegated_state = desc.resolve_alias("delegated")
        valid_from = [desc.resolve_state_ref(r) for r in block_d_cmd.from_states]
        assert delegated_state in valid_from
        assert desc.resolve_state_ref(block_d_cmd.to) == desc.resolve_alias(
            "blocked_delegated"
        )

    def test_t_ec_03_double_delegation_attempt(self):
        """T-EC-03: Double delegation attempt.

        Scenario: delegate command while another item is in delegated state.
        Expected: Rejected by no_in_progress_items invariant
        (delegated status = in_progress).
        """
        wi = _make_work_item_data()
        candidate = _make_candidate()
        cr = _make_candidate_result(selected=candidate)

        # Another item already in progress
        q = MockQuerier(count=1)
        engine, deps = _build_engine(work_item_data=wi, candidate_result=cr, querier=q)
        result = engine.process_delegation()

        assert result.status == EngineStatus.INVARIANT_FAILED
        assert "no_in_progress_items" in result.invariant_result.failed_invariants

    def test_t_ec_04_escalation_from_delegated(self):
        """T-EC-04: Escalation directly from delegated state.

        Scenario: escalate executed from delegated (emergency path).
        Expected: Allowed — delegated is in escalate.from[] per workflow.yaml.
        """
        desc = _make_descriptor()

        escalate_cmd = desc.get_command("escalate")
        delegated_state = desc.resolve_alias("delegated")
        valid_from = [desc.resolve_state_ref(r) for r in escalate_cmd.from_states]
        assert delegated_state in valid_from, (
            "escalate should accept delegated as a from-state (emergency path)"
        )

        # Verify via process_transition
        wi = _make_work_item_data(status="in-progress", stage="in_progress")
        engine, deps = _build_engine(work_item_data=wi)

        result = engine.process_transition("WL-1", "escalated")
        assert result.status == EngineStatus.SUCCESS
        assert result.command_name == "escalate"

    def test_t_ec_05_de_escalate_without_producer_comment(self):
        """T-EC-05: De-escalate without Producer comment.

        Scenario: de_escalate executed with no Producer comment.
        Expected: Command succeeds — no invariant requires Producer comment
        on de_escalate. The Producer is the actor, which is sufficient.

        Note: Both ``de_escalate`` and ``retry_delegation`` transition from
        ``escalated`` to ``plan`` (same target stage). The engine's
        ``_find_transition_command`` returns whichever it encounters first
        in dict iteration order. Both are valid and have no pre-invariants
        that block them here.
        """
        desc = _make_descriptor()

        de_esc_cmd = desc.get_command("de_escalate")

        # No pre or post invariants on de_escalate
        assert len(de_esc_cmd.pre) == 0
        assert len(de_esc_cmd.post) == 0

        # Verify via process_transition — both de_escalate and
        # retry_delegation target plan_complete, so accept either.
        wi = _make_work_item_data(
            status="blocked",
            stage="escalated",
            comments=[],  # No Producer comment
        )
        engine, deps = _build_engine(work_item_data=wi)

        result = engine.process_transition("WL-1", "plan_complete")
        assert result.status == EngineStatus.SUCCESS
        assert result.command_name in ("de_escalate", "retry_delegation"), (
            f"Expected de_escalate or retry_delegation, got {result.command_name}"
        )

    def test_t_ec_06_audit_result_on_non_review_state(self):
        """T-EC-06: audit_result from wrong state rejected.

        Scenario: audit_result attempted from delegated state.
        Expected: Rejected — delegated not in audit_result.from[] (only review).
        """
        desc = _make_descriptor()

        audit_cmd = desc.get_command("audit_result")
        delegated_state = desc.resolve_alias("delegated")
        valid_from = [desc.resolve_state_ref(r) for r in audit_cmd.from_states]
        assert delegated_state not in valid_from, (
            "audit_result should only be valid from review state"
        )

        # Verify via process_transition — should be rejected
        wi = _make_work_item_data(status="in-progress", stage="in_progress")
        engine, deps = _build_engine(work_item_data=wi)

        # Try to transition to audit_passed from delegated
        result = engine.process_transition("WL-1", "audit_passed")
        # The engine will look for a command from (in-progress, in_progress) to
        # audit_passed — audit_result requires from=review, so no match.
        # But escalate goes to escalated, not audit_passed.
        # So this should be rejected.
        assert result.status == EngineStatus.REJECTED

    def test_t_ec_07_minimal_valid_descriptor(self):
        """T-EC-07: Minimal valid descriptor.

        Scenario: Descriptor with 1 status, 1 stage, 1 state, 1 command,
        1 invariant, 1 role.
        Expected: Engine can be constructed and operates correctly.
        """
        minimal_desc = WorkflowDescriptor(
            version="1.0.0",
            metadata=Metadata(
                name="minimal",
                description="Minimal workflow",
                owner="test",
                roles=(Role(name="Admin"),),
            ),
            statuses=("open",),
            stages=("todo",),
            states={"ready": StateTuple(status="open", stage="todo")},
            invariants=(
                Invariant(
                    name="always_true",
                    description="Always passes",
                    when=("pre",),
                    logic="",  # No logic = always passes
                ),
            ),
            commands={
                "delegate": Command(
                    name="delegate",
                    description="Delegate work",
                    from_states=("ready",),
                    to="ready",
                    actor="Admin",
                    pre=("always_true",),
                    dispatch_map={"ready": 'echo "{id}"'},
                ),
            },
        )

        wi = _make_work_item_data(status="open", stage="todo")
        candidate = _make_candidate(status="open", stage="todo")
        cr = _make_candidate_result(selected=candidate)

        engine, deps = _build_engine(
            descriptor=minimal_desc,
            work_item_data=wi,
            candidate_result=cr,
        )
        result = engine.process_delegation()

        assert result.status == EngineStatus.SUCCESS
        assert result.work_item_id == "WL-1"

    def test_t_ec_08_large_descriptor_performance(self):
        """T-EC-08: Large descriptor performance.

        Scenario: Descriptor with 50+ commands, 20+ states, 30+ invariants.
        Expected: Engine construction and delegation completes in < 1 second.
        """
        # Generate a large descriptor
        large_states = {}
        for i in range(25):
            large_states[f"state_{i}"] = StateTuple(status="open", stage=f"stage_{i}")

        large_invariants = []
        for i in range(35):
            large_invariants.append(
                Invariant(
                    name=f"inv_{i}",
                    description=f"Invariant {i}",
                    when=("pre",),
                    logic="",  # No logic = always passes
                )
            )

        large_commands = {}
        # Create delegate command that references the first state
        large_commands["delegate"] = Command(
            name="delegate",
            description="Delegate",
            from_states=tuple(f"state_{i}" for i in range(25)),
            to="state_0",
            actor="Admin",
            pre=tuple(f"inv_{i}" for i in range(10)),  # 10 pre-invariants
            dispatch_map={
                f"state_{i}": f'echo "dispatch {i} {{id}}"' for i in range(25)
            },
        )

        # Add 50+ additional commands
        for i in range(55):
            src = i % 25
            dst = (i + 1) % 25
            large_commands[f"cmd_{i}"] = Command(
                name=f"cmd_{i}",
                description=f"Command {i}",
                from_states=(f"state_{src}",),
                to=f"state_{dst}",
                actor="Admin",
            )

        large_desc = WorkflowDescriptor(
            version="1.0.0",
            metadata=Metadata(
                name="large",
                description="Large workflow",
                owner="test",
                roles=(Role(name="Admin"),),
            ),
            statuses=("open",),
            stages=tuple(f"stage_{i}" for i in range(25)),
            states=large_states,
            invariants=tuple(large_invariants),
            commands=large_commands,
        )

        wi = _make_work_item_data(status="open", stage="stage_0")
        candidate = _make_candidate(status="open", stage="stage_0")
        cr = _make_candidate_result(selected=candidate)

        start = time.monotonic()
        engine, deps = _build_engine(
            descriptor=large_desc,
            work_item_data=wi,
            candidate_result=cr,
        )
        result = engine.process_delegation()
        elapsed = time.monotonic() - start

        assert result.status == EngineStatus.SUCCESS
        assert elapsed < 1.0, f"Large descriptor took {elapsed:.3f}s (expected < 1s)"
