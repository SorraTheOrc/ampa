"""Tests for _format_in_progress_items() parsing logic and skip reason display.

Covers acceptance criteria from SA-0MLZSJP7T0FR5F90:
  - Empty string returns []
  - 'No in-progress work items found' returns []
  - Error messages return []
  - Real SA- item lines are correctly extracted
  - _build_dry_run_report() produces idle messaging when items list is empty

Covers acceptance criteria from SA-0MLZSJZMR15ZVSR3:
  - Report includes invariant failure reasons when delegation is skipped
  - Report says 'no candidates returned' when no candidates exist
  - Report lists rejection reasons when candidates are rejected
  - Busy format still works when there ARE actual in-progress items
"""

import pytest

from ampa.delegation import _format_in_progress_items, _build_dry_run_report


class TestFormatInProgressItems:
    """Unit tests for _format_in_progress_items()."""

    def test_empty_string_returns_empty_list(self):
        assert _format_in_progress_items("") == []

    def test_none_returns_empty_list(self):
        assert _format_in_progress_items(None) == []  # type: ignore[arg-type]

    def test_no_items_message_returns_empty_list(self):
        assert _format_in_progress_items("No in-progress work items found") == []

    def test_error_message_returns_empty_list(self):
        assert _format_in_progress_items("Error: connection refused") == []

    def test_whitespace_only_returns_empty_list(self):
        assert _format_in_progress_items("   \n  \n  ") == []

    def test_random_text_returns_empty_list(self):
        assert _format_in_progress_items("some random output text") == []

    def test_single_real_item(self):
        text = "- SA-ABC123 Fix the bug (status: in-progress)"
        result = _format_in_progress_items(text)
        assert len(result) == 1
        assert "SA-ABC123" in result[0]

    def test_multiple_real_items(self):
        text = "- SA-001 First item\n- SA-002 Second item\n"
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_items_with_tree_characters(self):
        text = "├ - SA-001 First item\n└ - SA-002 Second item\n"
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_mixed_lines_only_extracts_items(self):
        text = (
            "In-progress work items:\n"
            "- SA-001 First item\n"
            "Some other text\n"
            "- SA-002 Second item\n"
        )
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_no_items_message_with_extra_whitespace(self):
        text = "  No in-progress work items found  \n"
        assert _format_in_progress_items(text) == []


class TestBuildDryRunReportIdleBranch:
    """Verify _build_dry_run_report() idle path when no in-progress items."""

    def test_idle_report_when_empty_in_progress(self):
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" not in report
        assert "idle" in report.lower() or "(none)" in report.lower()

    def test_idle_report_with_no_items_message(self):
        report = _build_dry_run_report(
            in_progress_output="No in-progress work items found",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" not in report

    def test_busy_report_with_real_items(self):
        report = _build_dry_run_report(
            in_progress_output="- SA-001 Working on something",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" in report
        assert "SA-001" in report

    def test_idle_report_includes_candidates(self):
        candidates = [
            {"id": "SA-42", "title": "Do thing", "status": "open", "priority": "high"},
        ]
        report = _build_dry_run_report(
            in_progress_output="No in-progress work items found",
            candidates=candidates,
            top_candidate=candidates[0],
        )
        assert "Agents are currently busy" not in report
        assert "SA-42" in report
        assert "Do thing" in report


class TestReportSkipReasons:
    """Verify _build_dry_run_report() surfaces skip/rejection reasons."""

    def test_invariant_failure_included_in_report(self):
        """When delegation is skipped due to invariant failure, the report
        includes the invariant name and failure reason."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[
                {"id": "SA-10", "title": "Some task", "status": "open"},
            ],
            top_candidate={"id": "SA-10", "title": "Some task", "status": "open"},
            skip_reasons=[
                "Delegation skipped: invariant requires_acceptance_criteria failed"
            ],
        )
        assert "Agents are currently busy" not in report
        assert "requires_acceptance_criteria" in report
        assert "Delegation skip reasons:" in report

    def test_no_candidates_report_text(self):
        """When no candidates exist, the report says so clearly."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            skip_reasons=["Delegation idle: no candidates returned"],
        )
        assert "no candidates returned" in report
        assert "Delegation skip reasons:" in report

    def test_rejection_reasons_listed(self):
        """When candidates are rejected, the report lists rejection reasons."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[
                {"id": "SA-20", "title": "Rejected task", "status": "open"},
            ],
            top_candidate=None,
            rejections=[
                {
                    "id": "SA-20",
                    "title": "Rejected task",
                    "reason": "stage 'closed' is not delegatable",
                },
            ],
        )
        assert "Rejected candidates:" in report
        assert "SA-20" in report
        assert "stage 'closed' is not delegatable" in report

    def test_multiple_rejections_listed(self):
        """Multiple rejection reasons are all surfaced."""
        rejections = [
            {
                "id": "SA-A",
                "title": "Task A",
                "reason": "stage 'closed' is not delegatable",
            },
            {"id": "SA-B", "title": "Task B", "reason": "do-not-delegate tag"},
        ]
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            rejections=rejections,
        )
        assert "SA-A" in report
        assert "SA-B" in report
        assert "stage 'closed' is not delegatable" in report
        assert "do-not-delegate tag" in report

    def test_busy_format_unchanged_with_skip_reasons(self):
        """Even if skip_reasons are passed, the busy format is used when
        there ARE actual in-progress items (regression check)."""
        report = _build_dry_run_report(
            in_progress_output="- SA-001 Working on something",
            candidates=[],
            top_candidate=None,
            skip_reasons=["should not appear"],
            rejections=[{"id": "SA-X", "title": "X", "reason": "should not appear"}],
        )
        assert "Agents are currently busy" in report
        assert "SA-001" in report
        assert "should not appear" not in report

    def test_no_skip_reasons_produces_original_report(self):
        """When no skip_reasons or rejections are passed, the report is
        identical to the original format (backward compatibility)."""
        report_without = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
        )
        report_with_none = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            skip_reasons=None,
            rejections=None,
        )
        assert report_without == report_with_none

    def test_empty_skip_reasons_list_produces_original_report(self):
        """Empty lists for skip_reasons/rejections don't add extra sections."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            skip_reasons=[],
            rejections=[],
        )
        assert "Delegation skip reasons:" not in report
        assert "Rejected candidates:" not in report

    def test_skip_reason_with_candidates_and_rejections(self):
        """Combined skip reasons and rejections both appear in the report."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[
                {"id": "SA-30", "title": "Good task", "status": "open"},
            ],
            top_candidate=None,
            skip_reasons=["invariant requires_work_item_context failed"],
            rejections=[
                {"id": "SA-30", "title": "Good task", "reason": "missing description"},
            ],
        )
        assert "Delegation skip reasons:" in report
        assert "requires_work_item_context" in report
        assert "Rejected candidates:" in report
        assert "missing description" in report
