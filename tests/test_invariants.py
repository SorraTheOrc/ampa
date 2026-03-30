"""Unit tests for ampa.engine.invariants — invariant evaluator.

Covers: all passing, single failure, multiple failures, missing invariant name,
each expression type (length, regex, membership, tag exclusion, count, compound),
work item data adapter, summary formatting.
"""

from __future__ import annotations

from typing import Any

import pytest

from ampa.engine.descriptor import Invariant
from ampa.engine.invariants import (
    InvariantEvaluator,
    InvariantResult,
    NullQuerier,
    SingleInvariantResult,
    WorkItemQuerier,
    evaluate_logic,
    extract_work_item_fields,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_invariant(
    name: str, logic: str, when: tuple[str, ...] = ("pre",)
) -> Invariant:
    return Invariant(name=name, description=f"Test: {name}", when=when, logic=logic)


def _make_work_item(
    description: str = "",
    tags: list[str] | None = None,
    stage: str = "idea",
    status: str = "open",
    comments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a mock wl show JSON output."""
    wi = {
        "workItem": {
            "id": "TEST-001",
            "title": "Test item",
            "description": description,
            "tags": tags or [],
            "stage": stage,
            "status": status,
            "assignee": "",
            "priority": "medium",
        },
        "comments": comments or [],
    }
    return wi


class MockQuerier:
    """Mock querier that returns a configurable count."""

    def __init__(self, count: int = 0):
        self._count = count

    def count_in_progress(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# Test: Work item data adapter
# ---------------------------------------------------------------------------


class TestExtractWorkItemFields:
    def test_basic_extraction(self) -> None:
        wi = _make_work_item(
            description="Hello world",
            tags=["a", "b"],
            stage="plan_complete",
            status="open",
        )
        fields = extract_work_item_fields(wi)
        assert fields["description"] == "Hello world"
        assert fields["tags"] == ["a", "b"]
        assert fields["stage"] == "plan_complete"
        assert fields["status"] == "open"

    def test_comments_concatenation(self) -> None:
        wi = _make_work_item(
            comments=[
                {"comment": "First comment"},
                {"comment": "Second comment"},
            ]
        )
        fields = extract_work_item_fields(wi)
        assert "First comment" in fields["comments_text"]
        assert "Second comment" in fields["comments_text"]

    def test_flat_work_item(self) -> None:
        """Handle work item without nested workItem key."""
        wi = {
            "id": "TEST-002",
            "description": "Flat item",
            "tags": [],
            "stage": "idea",
            "status": "open",
        }
        fields = extract_work_item_fields(wi)
        assert fields["description"] == "Flat item"

    def test_empty_tags_null(self) -> None:
        wi = {"workItem": {"description": "", "tags": None, "stage": "", "status": ""}}
        fields = extract_work_item_fields(wi)
        assert fields["tags"] == []

    def test_comments_with_body_key(self) -> None:
        wi = _make_work_item(comments=[{"body": "Body text"}])
        fields = extract_work_item_fields(wi)
        assert "Body text" in fields["comments_text"]


# ---------------------------------------------------------------------------
# Test: Length expression
# ---------------------------------------------------------------------------


class TestLengthExpression:
    def test_length_passes(self) -> None:
        fields = {"description": "A" * 101}
        passed, reason = evaluate_logic(
            "length(description) > 100", fields, NullQuerier()
        )
        assert passed is True

    def test_length_fails(self) -> None:
        fields = {"description": "Short"}
        passed, reason = evaluate_logic(
            "length(description) > 100", fields, NullQuerier()
        )
        assert passed is False
        assert "length(description) is 5" in reason

    def test_length_exact_boundary(self) -> None:
        fields = {"description": "A" * 100}
        passed, _ = evaluate_logic("length(description) > 100", fields, NullQuerier())
        assert passed is False  # 100 is not > 100

    def test_length_missing_field(self) -> None:
        fields = {}
        passed, _ = evaluate_logic("length(description) > 0", fields, NullQuerier())
        assert passed is False  # empty string has length 0, not > 0


# ---------------------------------------------------------------------------
# Test: Regex expression
# ---------------------------------------------------------------------------


class TestRegexExpression:
    def test_regex_matches_description(self) -> None:
        fields = {"description": "PRD: https://example.com"}
        # Patterns from YAML have double-backslash escaping (YAML single-quoted)
        # The evaluator un-escapes one level before applying regex
        passed, _ = evaluate_logic(
            'regex(description, "PRD:\\\\s*https?://")', fields, NullQuerier()
        )
        assert passed is True

    def test_regex_no_match(self) -> None:
        fields = {"description": "No link here"}
        passed, reason = evaluate_logic(
            'regex(description, "PRD:\\\\s*https?://")', fields, NullQuerier()
        )
        assert passed is False
        assert "did not match" in reason

    def test_regex_matches_comments(self) -> None:
        fields = {"comments_text": "Approved by admin"}
        passed, _ = evaluate_logic(
            'regex(comments, "Approved by\\\\s+\\\\w+")', fields, NullQuerier()
        )
        assert passed is True

    def test_regex_case_insensitive_in_pattern(self) -> None:
        fields = {"comments_text": "AMPA Audit Result: pass"}
        passed, _ = evaluate_logic(
            'regex(comments, "(?i)AMPA Audit Result")', fields, NullQuerier()
        )
        assert passed is True

    def test_regex_acceptance_criteria(self) -> None:
        fields = {"description": "## Acceptance Criteria\n- [ ] First item"}
        passed, _ = evaluate_logic(
            'regex(description, "(?i)(acceptance criteria|\\\\- \\\\[[ x]\\\\])")',
            fields,
            NullQuerier(),
        )
        assert passed is True

    def test_regex_checkbox_match(self) -> None:
        fields = {"description": "Requirements:\n- [x] Done\n- [ ] Pending"}
        passed, _ = evaluate_logic(
            'regex(description, "(?i)(acceptance criteria|\\\\- \\\\[[ x]\\\\])")',
            fields,
            NullQuerier(),
        )
        assert passed is True


# ---------------------------------------------------------------------------
# Test: Membership expression (stage in [...])
# ---------------------------------------------------------------------------


class TestMembershipExpression:
    def test_stage_in_list(self) -> None:
        fields = {"stage": "idea"}
        passed, _ = evaluate_logic(
            'stage in ["idea", "intake_complete", "plan_complete"]',
            fields,
            NullQuerier(),
        )
        assert passed is True

    def test_stage_not_in_list(self) -> None:
        fields = {"stage": "in_review"}
        passed, reason = evaluate_logic(
            'stage in ["idea", "intake_complete", "plan_complete"]',
            fields,
            NullQuerier(),
        )
        assert passed is False
        assert "in_review" in reason


# ---------------------------------------------------------------------------
# Test: Tag exclusion expression
# ---------------------------------------------------------------------------


class TestTagExclusion:
    def test_tag_not_present(self) -> None:
        fields = {"tags": ["feature", "priority-high"]}
        passed, _ = evaluate_logic(
            '"do-not-delegate" not in tags', fields, NullQuerier()
        )
        assert passed is True

    def test_tag_present(self) -> None:
        fields = {"tags": ["do-not-delegate", "feature"]}
        passed, reason = evaluate_logic(
            '"do-not-delegate" not in tags', fields, NullQuerier()
        )
        assert passed is False
        assert "do-not-delegate" in reason


# ---------------------------------------------------------------------------
# Test: Compound AND expression
# ---------------------------------------------------------------------------


class TestCompoundExpression:
    def test_both_pass(self) -> None:
        fields = {"tags": ["feature"]}
        passed, _ = evaluate_logic(
            '"do-not-delegate" not in tags and "do_not_delegate" not in tags',
            fields,
            NullQuerier(),
        )
        assert passed is True

    def test_first_fails(self) -> None:
        fields = {"tags": ["do-not-delegate"]}
        passed, reason = evaluate_logic(
            '"do-not-delegate" not in tags and "do_not_delegate" not in tags',
            fields,
            NullQuerier(),
        )
        assert passed is False
        assert "do-not-delegate" in reason

    def test_second_fails(self) -> None:
        fields = {"tags": ["do_not_delegate"]}
        passed, reason = evaluate_logic(
            '"do-not-delegate" not in tags and "do_not_delegate" not in tags',
            fields,
            NullQuerier(),
        )
        assert passed is False
        assert "do_not_delegate" in reason


# ---------------------------------------------------------------------------
# Test: Count expression
# ---------------------------------------------------------------------------


class TestCountExpression:
    def test_count_zero_passes(self) -> None:
        querier = MockQuerier(count=0)
        fields = {}
        passed, _ = evaluate_logic(
            'count(work_items, status="in_progress") == 0', fields, querier
        )
        assert passed is True

    def test_count_nonzero_fails(self) -> None:
        querier = MockQuerier(count=2)
        fields = {}
        passed, reason = evaluate_logic(
            'count(work_items, status="in_progress") == 0', fields, querier
        )
        assert passed is False
        assert "is 2" in reason

    def test_count_calls_querier(self) -> None:
        querier = MockQuerier(count=3)
        fields = {}
        passed, _ = evaluate_logic(
            'count(work_items, status="in_progress") == 0', fields, querier
        )
        assert passed is False


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_logic(self) -> None:
        fields = {}
        passed, reason = evaluate_logic("", fields, NullQuerier())
        assert passed is True
        assert "always passes" in reason

    def test_unrecognized_expression(self) -> None:
        fields = {}
        passed, reason = evaluate_logic(
            "some_unknown_function(x, y)", fields, NullQuerier()
        )
        assert passed is True
        assert "Unrecognized" in reason


# ---------------------------------------------------------------------------
# Test: InvariantEvaluator class
# ---------------------------------------------------------------------------


class TestInvariantEvaluator:
    @pytest.fixture
    def invariants(self) -> list[Invariant]:
        return [
            _make_invariant("has_description", "length(description) > 10"),
            _make_invariant(
                "has_ac",
                'regex(description, "(?i)(acceptance criteria|\\\\- \\\\[[ x]\\\\])")',
            ),
            _make_invariant(
                "valid_stage",
                'stage in ["idea", "intake_complete", "plan_complete"]',
            ),
            _make_invariant(
                "not_blocked",
                '"do-not-delegate" not in tags',
            ),
            _make_invariant(
                "no_wip",
                'count(work_items, status="in_progress") == 0',
            ),
        ]

    def test_all_pass(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants, querier=MockQuerier(0))
        wi = _make_work_item(
            description="A long description with ## Acceptance Criteria\n- [ ] Item",
            tags=[],
            stage="idea",
        )
        result = evaluator.evaluate(
            ["has_description", "valid_stage", "not_blocked", "no_wip"],
            wi,
        )
        assert result.passed is True
        assert len(result.failed_invariants) == 0

    def test_single_failure_fail_fast(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants, querier=MockQuerier(0))
        wi = _make_work_item(description="Short", stage="idea")
        result = evaluator.evaluate(
            ["has_description", "valid_stage"],
            wi,
            fail_fast=True,
        )
        assert result.passed is False
        # fail_fast should stop after first failure
        assert len(result.results) == 1
        assert result.results[0].name == "has_description"

    def test_multiple_failures_evaluate_all(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants, querier=MockQuerier(5))
        wi = _make_work_item(
            description="Short",
            tags=["do-not-delegate"],
            stage="in_review",
        )
        result = evaluator.evaluate(
            ["has_description", "valid_stage", "not_blocked", "no_wip"],
            wi,
            fail_fast=False,
        )
        assert result.passed is False
        assert len(result.failed_invariants) == 4

    def test_unknown_invariant(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants)
        wi = _make_work_item()
        with pytest.raises(KeyError, match="Unknown invariant 'nonexistent'"):
            evaluator.evaluate(["nonexistent"], wi)

    def test_summary_all_pass(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants, querier=MockQuerier(0))
        wi = _make_work_item(
            description="A" * 101 + "\n## Acceptance Criteria\n- [ ] Item",
            stage="idea",
        )
        result = evaluator.evaluate(["has_description", "valid_stage"], wi)
        assert "passed" in result.summary().lower()

    def test_summary_with_failures(self, invariants: list[Invariant]) -> None:
        evaluator = InvariantEvaluator(invariants, querier=MockQuerier(0))
        wi = _make_work_item(description="Short", stage="done")
        result = evaluator.evaluate(
            ["has_description", "valid_stage"],
            wi,
            fail_fast=False,
        )
        summary = result.summary()
        assert "2 of 2" in summary
        assert "has_description" in summary
        assert "valid_stage" in summary


# ---------------------------------------------------------------------------
# Test: InvariantResult data class
# ---------------------------------------------------------------------------


class TestInvariantResult:
    def test_passed_result(self) -> None:
        result = InvariantResult(
            passed=True,
            results=(SingleInvariantResult(name="a", passed=True, reason="ok"),),
        )
        assert result.passed is True
        assert result.failed_invariants == []

    def test_failed_result(self) -> None:
        result = InvariantResult(
            passed=False,
            results=(
                SingleInvariantResult(name="a", passed=True, reason="ok"),
                SingleInvariantResult(name="b", passed=False, reason="bad"),
            ),
        )
        assert result.passed is False
        assert result.failed_invariants == ["b"]

    def test_summary_format(self) -> None:
        result = InvariantResult(
            passed=False,
            results=(
                SingleInvariantResult(name="x", passed=False, reason="failed check"),
            ),
        )
        summary = result.summary()
        assert "1 of 1" in summary
        assert "x: failed check" in summary


# ---------------------------------------------------------------------------
# Test: Real workflow invariants against realistic data
# ---------------------------------------------------------------------------


class TestRealWorkflowInvariants:
    """Test using the actual invariant definitions from workflow.yaml."""

    @pytest.fixture
    def evaluator(self) -> InvariantEvaluator:
        """Build evaluator with workflow.yaml invariants."""
        from ampa.engine.descriptor import load_descriptor
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        desc = load_descriptor(
            repo / "docs" / "workflow" / "workflow.yaml",
            schema_path=repo / "docs" / "workflow" / "workflow-schema.json",
        )
        return InvariantEvaluator(desc.invariants, querier=MockQuerier(0))

    def test_delegate_preconditions_pass(self, evaluator: InvariantEvaluator) -> None:
        wi = _make_work_item(
            description=(
                "A detailed feature description with enough context for "
                "autonomous implementation.\n\n"
                "## Acceptance Criteria\n"
                "- [ ] First criterion\n"
                "- [ ] Second criterion\n"
            ),
            tags=[],
            stage="plan_complete",
        )
        result = evaluator.evaluate(
            [
                "requires_work_item_context",
                "requires_acceptance_criteria",
                "requires_stage_for_delegation",
                "not_do_not_delegate",
                "no_in_progress_items",
            ],
            wi,
        )
        assert result.passed is True

    def test_delegate_preconditions_fail_short_description(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            description="Too short",
            tags=[],
            stage="plan_complete",
        )
        result = evaluator.evaluate(
            ["requires_work_item_context"],
            wi,
        )
        assert result.passed is False
        assert "requires_work_item_context" in result.failed_invariants

    def test_delegate_preconditions_fail_do_not_delegate(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            description="A" * 200 + "\n- [ ] criterion",
            tags=["do-not-delegate"],
            stage="plan_complete",
        )
        result = evaluator.evaluate(
            ["not_do_not_delegate"],
            wi,
        )
        assert result.passed is False

    def test_delegate_preconditions_fail_wrong_stage(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            description="A" * 200 + "\n- [ ] criterion",
            tags=[],
            stage="in_review",
        )
        result = evaluator.evaluate(
            ["requires_stage_for_delegation"],
            wi,
        )
        assert result.passed is False

    def test_post_invariant_requires_approvals(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            description="Feature",
            comments=[{"comment": "Approved by producer on 2026-02-20"}],
        )
        result = evaluator.evaluate(["requires_approvals"], wi)
        assert result.passed is True

    def test_post_invariant_requires_approvals_missing(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            description="Feature",
            comments=[{"comment": "Looks good"}],
        )
        result = evaluator.evaluate(["requires_approvals"], wi)
        assert result.passed is False


# ---------------------------------------------------------------------------
# Test: Audit-specific invariants (F2)
# ---------------------------------------------------------------------------


class TestAuditInvariants:
    """Verify audit-specific invariants from workflow.yaml evaluate correctly.

    Invariants tested:
    - requires_audit_result: regex(comments, "(?i)AMPA Audit Result")
    - audit_recommends_closure: regex(comments, "(?i)(can this item be closed?\\s*yes|...)")
    - audit_does_not_recommend_closure: regex(comments, "(?i)can this item be closed?\\s*no")
    """

    @pytest.fixture
    def evaluator(self) -> InvariantEvaluator:
        from ampa.engine.descriptor import load_descriptor
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        desc = load_descriptor(
            repo / "docs" / "workflow" / "workflow.yaml",
            schema_path=repo / "docs" / "workflow" / "workflow-schema.json",
        )
        return InvariantEvaluator(desc.invariants, querier=MockQuerier(0))

    # -- requires_audit_result -------------------------------------------------

    def test_requires_audit_result_passes(self, evaluator: InvariantEvaluator) -> None:
        wi = _make_work_item(
            comments=[{"comment": "AMPA Audit Result: All criteria met."}],
        )
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is True

    def test_requires_audit_result_case_insensitive(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "ampa audit result: pass"}],
        )
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is True

    def test_requires_audit_result_no_comments(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(comments=[])
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is False

    def test_requires_audit_result_partial_match_fails(
        self, evaluator: InvariantEvaluator
    ) -> None:
        """'AMPA Audit' without 'Result' should not match."""
        wi = _make_work_item(
            comments=[{"comment": "AMPA Audit completed for this item."}],
        )
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is False

    def test_requires_audit_result_in_multiple_comments(
        self, evaluator: InvariantEvaluator
    ) -> None:
        """Audit result present in second comment should still match."""
        wi = _make_work_item(
            comments=[
                {"comment": "Agent started work."},
                {"comment": "AMPA Audit Result: 5/5 criteria met."},
            ],
        )
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is True

    def test_requires_audit_result_mixed_case(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Ampa AUDIT result: pass"}],
        )
        result = evaluator.evaluate(["requires_audit_result"], wi)
        assert result.passed is True

    # -- audit_recommends_closure ----------------------------------------------

    def test_recommends_closure_explicit_yes(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Can this item be closed? Yes"}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is True

    def test_recommends_closure_all_criteria_met(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "All acceptance criteria are met."}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is True

    def test_recommends_closure_all_criteria_satisfied(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "All acceptance criteria have been satisfied."}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is True

    def test_recommends_closure_case_insensitive(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "CAN THIS ITEM BE CLOSED? YES"}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is True

    def test_recommends_closure_whitespace_variations(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Can this item be closed?   yes"}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is True

    def test_recommends_closure_no_match(self, evaluator: InvariantEvaluator) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Audit found some issues."}],
        )
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is False

    def test_recommends_closure_no_comments(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(comments=[])
        result = evaluator.evaluate(["audit_recommends_closure"], wi)
        assert result.passed is False

    # -- audit_does_not_recommend_closure --------------------------------------

    def test_does_not_recommend_closure_explicit_no(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Can this item be closed? No"}],
        )
        result = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result.passed is True

    def test_does_not_recommend_closure_case_insensitive(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "can this item be closed? no"}],
        )
        result = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result.passed is True

    def test_does_not_recommend_closure_whitespace(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(
            comments=[{"comment": "Can this item be closed?    no"}],
        )
        result = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result.passed is True

    def test_does_not_recommend_closure_no_match(
        self, evaluator: InvariantEvaluator
    ) -> None:
        """When audit says 'yes', does_not_recommend should not match."""
        wi = _make_work_item(
            comments=[{"comment": "Can this item be closed? Yes"}],
        )
        result = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result.passed is False

    def test_does_not_recommend_no_comments(
        self, evaluator: InvariantEvaluator
    ) -> None:
        wi = _make_work_item(comments=[])
        result = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result.passed is False

    # -- Edge cases ------------------------------------------------------------

    def test_audit_result_returns_false_not_error_on_no_comments(
        self, evaluator: InvariantEvaluator
    ) -> None:
        """Invariant evaluation should return False (not raise) with no comments."""
        wi = _make_work_item(comments=[])
        result = evaluator.evaluate(
            ["requires_audit_result", "audit_recommends_closure"],
            wi,
            fail_fast=False,
        )
        assert result.passed is False
        # Both should be clean failures, not exceptions
        assert all(isinstance(r, SingleInvariantResult) for r in result.results)

    def test_multiple_audit_comments_latest_still_searchable(
        self, evaluator: InvariantEvaluator
    ) -> None:
        """All comments are concatenated, so any match wins.

        Note: the invariant does not enforce 'latest wins' -- it checks
        the concatenated text of all comments.  This test documents that
        behavior.
        """
        wi = _make_work_item(
            comments=[
                {"comment": "Can this item be closed? No"},
                {"comment": "Revised: Can this item be closed? Yes"},
            ],
        )
        # Both yes and no patterns exist -- both invariants will match
        result_yes = evaluator.evaluate(["audit_recommends_closure"], wi)
        result_no = evaluator.evaluate(["audit_does_not_recommend_closure"], wi)
        assert result_yes.passed is True
        assert result_no.passed is True
