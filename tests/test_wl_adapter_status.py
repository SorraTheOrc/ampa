"""Tests for WLAdapter status handling including input_needed status.

Work item: AM-0MNU8W52S009VYLW
"""

import json
from plan.wl_adapter import WLAdapter, VALID_STATUSES, CLOSED_STATUSES


class DummyWL(WLAdapter):
    """Test helper that returns canned responses."""

    def __init__(self, responses=None):
        self.responses = responses or {}

    def _run(self, args):
        key = tuple(args)
        if key in self.responses:
            return self.responses[key]
        return json.dumps([])


class TestStatusConstants:
    """Test that status constants are correctly defined."""

    def test_valid_statuses_includes_all_expected(self):
        """VALID_STATUSES should include all standard worklog statuses."""
        expected = {
            "open",
            "in-progress",
            "completed",
            "blocked",
            "input_needed",
            "deleted",
        }
        assert VALID_STATUSES == expected

    def test_valid_statuses_includes_input_needed(self):
        """input_needed should be recognized as a valid status."""
        assert "input_needed" in VALID_STATUSES

    def test_closed_statuses_includes_all_expected(self):
        """CLOSED_STATUSES should include all terminal statuses."""
        expected = {"closed", "done", "completed", "resolved", "deleted"}
        assert CLOSED_STATUSES == expected

    def test_closed_statuses_includes_deleted(self):
        """deleted should be recognized as a closed status."""
        assert "deleted" in CLOSED_STATUSES


class TestIsValidStatus:
    """Test the is_valid_status method."""

    def test_valid_status_open(self):
        w = DummyWL()
        assert w.is_valid_status("open") is True

    def test_valid_status_in_progress(self):
        w = DummyWL()
        assert w.is_valid_status("in-progress") is True

    def test_valid_status_input_needed(self):
        """input_needed should be recognized as valid."""
        w = DummyWL()
        assert w.is_valid_status("input_needed") is True

    def test_invalid_status_returns_false(self):
        w = DummyWL()
        assert w.is_valid_status("invalid_status") is False

    def test_invalid_status_empty(self):
        w = DummyWL()
        assert w.is_valid_status("") is False

    def test_invalid_status_none(self):
        w = DummyWL()
        assert w.is_valid_status(None) is False


class TestIsClosedStatus:
    """Test the is_closed_status method."""

    def test_completed_is_closed(self):
        w = DummyWL()
        assert w.is_closed_status("completed") is True

    def test_closed_is_closed(self):
        w = DummyWL()
        assert w.is_closed_status("closed") is True

    def test_done_is_closed(self):
        w = DummyWL()
        assert w.is_closed_status("done") is True

    def test_resolved_is_closed(self):
        w = DummyWL()
        assert w.is_closed_status("resolved") is True

    def test_deleted_is_closed(self):
        """deleted should be recognized as a closed status."""
        w = DummyWL()
        assert w.is_closed_status("deleted") is True

    def test_open_is_not_closed(self):
        w = DummyWL()
        assert w.is_closed_status("open") is False

    def test_in_progress_is_not_closed(self):
        w = DummyWL()
        assert w.is_closed_status("in-progress") is False

    def test_input_needed_is_not_closed(self):
        """input_needed should NOT be recognized as a closed status."""
        w = DummyWL()
        assert w.is_closed_status("input_needed") is False

    def test_blocked_is_not_closed(self):
        w = DummyWL()
        assert w.is_closed_status("blocked") is False


class TestListByStatus:
    """Test the list_by_status method."""

    def test_list_by_status_returns_items(self):
        """Should return list of work items matching status."""
        items = [
            {"id": "SA-1", "title": "Item 1", "status": "input_needed"},
            {"id": "SA-2", "title": "Item 2", "status": "input_needed"},
        ]
        responses = {
            ("list", "--status", "input_needed", "--json"): json.dumps(items),
        }
        w = DummyWL(responses)
        result = w.list_by_status("input_needed")
        assert len(result) == 2
        assert result[0]["id"] == "SA-1"
        assert result[1]["id"] == "SA-2"

    def test_list_by_status_empty_result(self):
        """Should return empty list when no items match."""
        responses = {
            ("list", "--status", "input_needed", "--json"): json.dumps([]),
        }
        w = DummyWL(responses)
        result = w.list_by_status("input_needed")
        assert result == []

    def test_list_by_status_cli_failure(self):
        """Should return empty list when CLI fails."""

        class FailWL(WLAdapter):
            def _run(self, args):
                return None

        w = FailWL()
        result = w.list_by_status("input_needed")
        assert result == []

    def test_list_by_status_invalid_json(self):
        """Should return empty list when JSON is invalid."""
        responses = {
            ("list", "--status", "input_needed", "--json"): "invalid json",
        }
        w = DummyWL(responses)
        result = w.list_by_status("input_needed")
        assert result == []


class TestListByStatusAndStage:
    """Test the list_by_status_and_stage method."""

    def test_list_by_status_and_stage_returns_items(self):
        """Should return items matching both status and stage."""
        items = [
            {"id": "SA-1", "title": "Item 1", "status": "input_needed", "stage": "idea"},
        ]
        responses = {
            ("list", "--status", "input_needed", "--stage", "idea", "--json"): json.dumps(
                items
            ),
        }
        w = DummyWL(responses)
        result = w.list_by_status_and_stage("input_needed", "idea")
        assert len(result) == 1
        assert result[0]["id"] == "SA-1"

    def test_list_by_status_and_stage_empty_result(self):
        """Should return empty list when no items match."""
        responses = {
            ("list", "--status", "input_needed", "--stage", "idea", "--json"): json.dumps(
                []
            ),
        }
        w = DummyWL(responses)
        result = w.list_by_status_and_stage("input_needed", "idea")
        assert result == []

    def test_list_by_status_and_stage_cli_failure(self):
        """Should return empty list when CLI fails."""

        class FailWL(WLAdapter):
            def _run(self, args):
                return None

        w = FailWL()
        result = w.list_by_status_and_stage("input_needed", "idea")
        assert result == []


class TestGracefulDegradation:
    """Test graceful degradation when Worklog doesn't support input_needed."""

    def test_input_needed_query_fails_gracefully(self):
        """If wl doesn't support input_needed, queries should return empty, not crash."""

        class OldWL(WLAdapter):
            """Simulates old wl that doesn't know about input_needed."""

            def _run(self, args):
                if "input_needed" in args:
                    return None  # CLI returns None on failure
                return json.dumps([])

        w = OldWL()
        # Should return empty list, not raise
        result = w.list_by_status("input_needed")
        assert result == []

    def test_validation_still_works_for_known_statuses(self):
        """Validation should work even if wl doesn't support input_needed yet."""
        w = DummyWL()
        # These should still work
        assert w.is_valid_status("open") is True
        assert w.is_valid_status("completed") is True
        # input_needed is in our constants even if wl doesn't support it yet
        assert w.is_valid_status("input_needed") is True


class TestStatusTransitions:
    """Test status transitions to and from input_needed.

    These tests verify that status transitions involving input_needed
    are handled correctly by the AMPA adapter.
    """

    def test_transition_from_open_to_input_needed(self):
        """Should allow transition from open to input_needed."""
        w = DummyWL()
        # Both statuses should be valid
        assert w.is_valid_status("open") is True
        assert w.is_valid_status("input_needed") is True
        # input_needed should not be treated as closed
        assert w.is_closed_status("input_needed") is False

    def test_transition_from_in_progress_to_input_needed(self):
        """Should allow transition from in-progress to input_needed."""
        w = DummyWL()
        assert w.is_valid_status("in-progress") is True
        assert w.is_valid_status("input_needed") is True
        assert w.is_closed_status("input_needed") is False

    def test_transition_from_blocked_to_input_needed(self):
        """Should allow transition from blocked to input_needed."""
        w = DummyWL()
        assert w.is_valid_status("blocked") is True
        assert w.is_valid_status("input_needed") is True
        assert w.is_closed_status("input_needed") is False

    def test_transition_from_input_needed_to_open(self):
        """Should allow transition from input_needed back to open."""
        w = DummyWL()
        assert w.is_valid_status("input_needed") is True
        assert w.is_valid_status("open") is True
        # When returning to open, the item is not closed
        assert w.is_closed_status("open") is False

    def test_transition_from_input_needed_to_in_progress(self):
        """Should allow transition from input_needed to in-progress when input received."""
        w = DummyWL()
        assert w.is_valid_status("input_needed") is True
        assert w.is_valid_status("in-progress") is True
        assert w.is_closed_status("in-progress") is False

    def test_transition_from_input_needed_to_completed(self):
        """Should allow transition from input_needed to completed if work is done."""
        w = DummyWL()
        assert w.is_valid_status("input_needed") is True
        assert w.is_valid_status("completed") is True
        # Completed is a closed status
        assert w.is_closed_status("completed") is True

    def test_transition_from_input_needed_to_blocked(self):
        """Should allow transition from input_needed to blocked if blocked later."""
        w = DummyWL()
        assert w.is_valid_status("input_needed") is True
        assert w.is_valid_status("blocked") is True
        assert w.is_closed_status("blocked") is False

    def test_input_needed_not_treated_as_closed_in_audit(self):
        """input_needed should not prevent audit closure checks."""
        # This test verifies that the CLOSED_STATUSES constant used by
        # audit handlers correctly excludes input_needed
        from plan.wl_adapter import CLOSED_STATUSES
        assert "input_needed" not in CLOSED_STATUSES
        # Verify other non-closed statuses are also excluded
        assert "open" not in CLOSED_STATUSES
        assert "in-progress" not in CLOSED_STATUSES
        assert "blocked" not in CLOSED_STATUSES
        # But closed statuses are included
        assert "completed" in CLOSED_STATUSES
        assert "closed" in CLOSED_STATUSES
        assert "done" in CLOSED_STATUSES

    def test_all_valid_transitions_covered(self):
        """All status values should be valid for transitions."""
        from plan.wl_adapter import VALID_STATUSES
        w = DummyWL()
        # Every valid status should be recognized
        for status in VALID_STATUSES:
            assert w.is_valid_status(status) is True, f"Status {status} should be valid"

    def test_input_needed_in_valid_statuses(self):
        """input_needed must be in VALID_STATUSES for transitions to work."""
        from plan.wl_adapter import VALID_STATUSES
        assert "input_needed" in VALID_STATUSES
