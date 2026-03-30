"""Tests for ampa.engine.candidates — candidate selection refactor.

Covers:
- WorkItemCandidate extraction from raw dicts
- Do-not-delegate tag filtering (tags, metadata, explicit field)
- From-state filtering against workflow descriptor
- Global blocker (in-progress items)
- CandidateSelector end-to-end: no candidates, all rejected, single valid,
  mixed valid/rejected, global blocker
- CandidateResult summary formatting
"""

from __future__ import annotations

import pytest
from pathlib import Path
from typing import Any

from ampa.engine.candidates import (
    CandidateResult,
    CandidateRejection,
    CandidateSelector,
    WorkItemCandidate,
    is_do_not_delegate,
    is_in_valid_from_state,
    get_valid_from_states,
    to_candidate,
)
from ampa.engine.descriptor import StateTuple, load_descriptor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "workflow" / "workflow.yaml"
)


@pytest.fixture
def descriptor():
    """Load the real workflow descriptor."""
    return load_descriptor(WORKFLOW_PATH)


class FakeFetcher:
    """Fake CandidateFetcher for testing."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []

    def fetch(self) -> list[dict[str, Any]]:
        return self.items


class FakeInProgressQuerier:
    """Fake InProgressQuerier for testing."""

    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count_in_progress(self) -> int:
        return self._count


class FailingFetcher:
    """Fetcher that raises an exception."""

    def fetch(self) -> list[dict[str, Any]]:
        raise RuntimeError("connection failed")


class FailingQuerier:
    """Querier that raises an exception."""

    def count_in_progress(self) -> int:
        raise RuntimeError("wl in_progress failed")


def _make_raw(
    id: str = "WL-1",
    title: str = "Test item",
    status: str = "open",
    stage: str = "plan_complete",
    priority: str = "medium",
    tags: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a raw work item dict matching wl next output."""
    d: dict[str, Any] = {
        "id": id,
        "title": title,
        "status": status,
        "stage": stage,
        "priority": priority,
    }
    if tags is not None:
        d["tags"] = tags
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# to_candidate tests
# ---------------------------------------------------------------------------


class TestToCandidate:
    def test_basic_extraction(self):
        raw = _make_raw(id="WL-42", title="My Task", status="open", stage="idea")
        c = to_candidate(raw)
        assert c.id == "WL-42"
        assert c.title == "My Task"
        assert c.status == "open"
        assert c.stage == "idea"

    def test_tags_from_list(self):
        raw = _make_raw(tags=["Feature", "urgent"])
        c = to_candidate(raw)
        assert c.tags == ("feature", "urgent")

    def test_tags_from_comma_string(self):
        raw = _make_raw(tags=None)
        raw["tags"] = "Feature, urgent"
        c = to_candidate(raw)
        assert c.tags == ("feature", "urgent")

    def test_empty_tags(self):
        raw = _make_raw(tags=[])
        c = to_candidate(raw)
        assert c.tags == ()

    def test_missing_id_returns_empty(self):
        raw = {"title": "No ID", "status": "open", "stage": "idea"}
        c = to_candidate(raw)
        assert c.id == ""

    def test_stage_fallback_to_state_key(self):
        raw = {"id": "WL-1", "title": "T", "status": "open", "state": "idea"}
        c = to_candidate(raw)
        assert c.stage == "idea"

    def test_status_and_stage_normalized_lowercase(self):
        raw = _make_raw(status="OPEN", stage="PLAN_COMPLETE")
        c = to_candidate(raw)
        assert c.status == "open"
        assert c.stage == "plan_complete"


# ---------------------------------------------------------------------------
# is_do_not_delegate tests
# ---------------------------------------------------------------------------


class TestIsDoNotDelegate:
    def test_tag_hyphenated(self):
        c = to_candidate(_make_raw(tags=["do-not-delegate"]))
        assert is_do_not_delegate(c) is True

    def test_tag_underscored(self):
        c = to_candidate(_make_raw(tags=["do_not_delegate"]))
        assert is_do_not_delegate(c) is True

    def test_no_tag(self):
        c = to_candidate(_make_raw(tags=["feature", "urgent"]))
        assert is_do_not_delegate(c) is False

    def test_empty_tags(self):
        c = to_candidate(_make_raw(tags=[]))
        assert is_do_not_delegate(c) is False

    def test_metadata_do_not_delegate(self):
        raw = _make_raw()
        raw["metadata"] = {"do_not_delegate": "true"}
        c = to_candidate(raw)
        assert is_do_not_delegate(c) is True

    def test_metadata_no_delegation(self):
        raw = _make_raw()
        raw["metadata"] = {"no_delegation": "yes"}
        c = to_candidate(raw)
        assert is_do_not_delegate(c) is True

    def test_explicit_field(self):
        raw = _make_raw()
        raw["do_not_delegate"] = True
        c = to_candidate(raw)
        assert is_do_not_delegate(c) is True

    def test_explicit_field_string_true(self):
        raw = _make_raw()
        raw["do_not_delegate"] = "true"
        c = to_candidate(raw)
        assert is_do_not_delegate(c) is True

    def test_metadata_falsy_value(self):
        raw = _make_raw()
        raw["metadata"] = {"do_not_delegate": "false"}
        c = to_candidate(raw)
        assert is_do_not_delegate(c) is False

    def test_tag_case_insensitive(self):
        """Tags are lowercased during extraction, so mixed case works."""
        c = to_candidate(_make_raw(tags=["Do-Not-Delegate"]))
        assert is_do_not_delegate(c) is True


# ---------------------------------------------------------------------------
# get_valid_from_states tests
# ---------------------------------------------------------------------------


class TestGetValidFromStates:
    def test_delegate_from_states(self, descriptor):
        states = get_valid_from_states(descriptor, "delegate")
        # delegate command: from [idea, intake, plan]
        # idea = (open, idea), intake = (open, intake_complete), plan = (open, plan_complete)
        assert StateTuple(status="open", stage="idea") in states
        assert StateTuple(status="open", stage="intake_complete") in states
        assert StateTuple(status="open", stage="plan_complete") in states
        assert len(states) == 3

    def test_unknown_command_raises(self, descriptor):
        with pytest.raises(KeyError, match="Unknown command"):
            get_valid_from_states(descriptor, "nonexistent")


# ---------------------------------------------------------------------------
# is_in_valid_from_state tests
# ---------------------------------------------------------------------------


class TestIsInValidFromState:
    def test_valid_state(self, descriptor):
        states = get_valid_from_states(descriptor, "delegate")
        c = to_candidate(_make_raw(status="open", stage="idea"))
        assert is_in_valid_from_state(c, states) is True

    def test_invalid_state(self, descriptor):
        states = get_valid_from_states(descriptor, "delegate")
        c = to_candidate(_make_raw(status="in_progress", stage="in_review"))
        assert is_in_valid_from_state(c, states) is False

    def test_all_delegatable_stages(self, descriptor):
        states = get_valid_from_states(descriptor, "delegate")
        for stage in ("idea", "intake_complete", "plan_complete"):
            c = to_candidate(_make_raw(status="open", stage=stage))
            assert is_in_valid_from_state(c, states) is True

    def test_non_delegatable_stages(self, descriptor):
        states = get_valid_from_states(descriptor, "delegate")
        for stage in ("delegated", "in_progress", "in_review", "done"):
            c = to_candidate(_make_raw(status="open", stage=stage))
            assert is_in_valid_from_state(c, states) is False


# ---------------------------------------------------------------------------
# CandidateSelector tests
# ---------------------------------------------------------------------------


class TestCandidateSelectorNoCandidates:
    def test_no_candidates(self, descriptor):
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher([]),
        )
        result = selector.select()
        assert result.selected is None
        assert not result.has_candidates
        assert "No candidates" in result.global_rejections[0]

    def test_fetcher_exception(self, descriptor):
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FailingFetcher(),
        )
        result = selector.select()
        assert result.selected is None
        assert "No candidates" in result.global_rejections[0]


class TestCandidateSelectorAllRejected:
    def test_all_do_not_delegate(self, descriptor):
        items = [
            _make_raw(id="WL-1", tags=["do-not-delegate"], stage="plan_complete"),
            _make_raw(id="WL-2", tags=["do_not_delegate"], stage="idea"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is None
        assert result.has_candidates
        assert len(result.rejections) == 2
        assert all("do-not-delegate" in r.reason for r in result.rejections)

    def test_all_wrong_stage(self, descriptor):
        items = [
            _make_raw(id="WL-1", stage="in_review", status="in_progress"),
            _make_raw(id="WL-2", stage="done", status="closed"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is None
        assert len(result.rejections) == 2
        assert all("not delegatable" in r.reason for r in result.rejections)

    def test_missing_id_rejected(self, descriptor):
        items = [{"title": "No ID", "status": "open", "stage": "idea"}]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is None
        assert len(result.rejections) == 1
        assert "missing id" in result.rejections[0].reason


class TestCandidateSelectorSingleValid:
    def test_single_valid_idea(self, descriptor):
        items = [_make_raw(id="WL-1", stage="idea", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-1"
        assert result.selected.stage == "idea"
        assert len(result.rejections) == 0

    def test_single_valid_plan_complete(self, descriptor):
        items = [_make_raw(id="WL-2", stage="plan_complete", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-2"


class TestCandidateSelectorMixed:
    def test_first_rejected_second_valid(self, descriptor):
        items = [
            _make_raw(id="WL-1", tags=["do-not-delegate"], stage="plan_complete"),
            _make_raw(id="WL-2", stage="intake_complete", status="open"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-2"
        assert len(result.rejections) == 1
        assert result.rejections[0].candidate.id == "WL-1"

    def test_wrong_stage_then_valid(self, descriptor):
        items = [
            _make_raw(id="WL-1", stage="in_review", status="in_progress"),
            _make_raw(id="WL-2", stage="idea", status="open"),
            _make_raw(id="WL-3", stage="plan_complete", status="open"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-2"  # First valid candidate
        assert len(result.rejections) == 1
        assert result.rejections[0].candidate.id == "WL-1"
        # WL-3 is also valid but not selected (first wins)
        assert len(result.candidates) == 3


class TestCandidateSelectorGlobalBlocker:
    def test_in_progress_blocks_all(self, descriptor):
        items = [_make_raw(id="WL-1", stage="plan_complete", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FakeInProgressQuerier(count=1),
        )
        result = selector.select()
        assert result.selected is None
        assert len(result.global_rejections) == 1
        assert "In-progress items exist" in result.global_rejections[0]
        # Candidates are still listed for audit trail
        assert result.has_candidates

    def test_multiple_in_progress(self, descriptor):
        items = [_make_raw(id="WL-1", stage="idea", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FakeInProgressQuerier(count=3),
        )
        result = selector.select()
        assert result.selected is None
        assert "3 item(s)" in result.global_rejections[0]

    def test_zero_in_progress_allows_delegation(self, descriptor):
        items = [_make_raw(id="WL-1", stage="plan_complete", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FakeInProgressQuerier(count=0),
        )
        result = selector.select()
        assert result.selected is not None

    def test_querier_exception_blocks(self, descriptor):
        items = [_make_raw(id="WL-1", stage="plan_complete", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FailingQuerier(),
        )
        result = selector.select()
        assert result.selected is None
        assert "query error" in result.global_rejections[0]


# ---------------------------------------------------------------------------
# CandidateResult tests
# ---------------------------------------------------------------------------


class TestCandidateResult:
    def test_summary_selected(self):
        c = WorkItemCandidate(
            id="WL-1",
            title="My Task",
            status="open",
            stage="plan_complete",
        )
        result = CandidateResult(selected=c, candidates=(c,))
        summary = result.summary()
        assert "WL-1" in summary
        assert "My Task" in summary

    def test_summary_no_candidates(self):
        result = CandidateResult(selected=None)
        assert "No candidates" in result.summary()

    def test_summary_with_rejections(self):
        c = WorkItemCandidate(
            id="WL-1",
            title="Blocked",
            status="open",
            stage="idea",
        )
        rej = CandidateRejection(candidate=c, reason="tagged do-not-delegate")
        result = CandidateResult(
            selected=None,
            candidates=(c,),
            rejections=(rej,),
        )
        summary = result.summary()
        assert "WL-1" in summary
        assert "do-not-delegate" in summary

    def test_summary_with_global_rejections(self):
        result = CandidateResult(
            selected=None,
            global_rejections=("In-progress items exist (2 item(s))",),
        )
        summary = result.summary()
        assert "Global blockers" in summary
        assert "In-progress" in summary

    def test_has_candidates_true(self):
        c = WorkItemCandidate(id="WL-1", title="T", status="open", stage="idea")
        result = CandidateResult(selected=None, candidates=(c,))
        assert result.has_candidates is True

    def test_has_candidates_false(self):
        result = CandidateResult(selected=None)
        assert result.has_candidates is False


# ---------------------------------------------------------------------------
# Integration: real workflow descriptor + selector
# ---------------------------------------------------------------------------


class TestRealWorkflowIntegration:
    """Tests using the actual workflow.yaml descriptor."""

    def test_full_selection_happy_path(self, descriptor):
        """A plan_complete candidate should be selected."""
        items = [
            _make_raw(id="WL-1", stage="plan_complete", status="open", priority="high"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FakeInProgressQuerier(count=0),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-1"
        assert result.selected.priority == "high"
        assert len(result.rejections) == 0
        assert len(result.global_rejections) == 0

    def test_idea_stage_is_delegatable(self, descriptor):
        """idea maps to (open, idea) which is a valid from state."""
        items = [_make_raw(id="WL-1", stage="idea", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None

    def test_intake_complete_is_delegatable(self, descriptor):
        """intake_complete maps to (open, intake_complete) via 'intake' alias."""
        items = [_make_raw(id="WL-1", stage="intake_complete", status="open")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is not None

    def test_delegated_stage_not_delegatable(self, descriptor):
        """delegated items should not be re-delegated."""
        items = [_make_raw(id="WL-1", stage="delegated", status="in_progress")]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
        )
        result = selector.select()
        assert result.selected is None
        assert len(result.rejections) == 1

    def test_mixed_scenario(self, descriptor):
        """Multiple candidates: do-not-delegate, wrong stage, valid."""
        items = [
            _make_raw(id="WL-1", tags=["do-not-delegate"], stage="plan_complete"),
            _make_raw(id="WL-2", stage="in_review", status="in_progress"),
            _make_raw(id="WL-3", stage="idea", status="open"),
        ]
        selector = CandidateSelector(
            descriptor=descriptor,
            fetcher=FakeFetcher(items),
            in_progress_querier=FakeInProgressQuerier(count=0),
        )
        result = selector.select()
        assert result.selected is not None
        assert result.selected.id == "WL-3"
        assert len(result.rejections) == 2
        reasons = [r.reason for r in result.rejections]
        assert any("do-not-delegate" in r for r in reasons)
        assert any("not delegatable" in r for r in reasons)
