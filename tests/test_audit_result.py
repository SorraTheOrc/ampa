"""Tests for ampa.audit.result — AuditResult dataclass and parser.

Covers:
- Successful parse with pass recommendation
- Successful parse with fail recommendation
- Malformed output (no markers)
- Empty output
- Multiple acceptance criteria evaluation
- Partial criteria verdicts
- Missing sections graceful handling
"""

from __future__ import annotations

import pytest

from ampa.audit.result import (
    AUDIT_REPORT_END,
    AUDIT_REPORT_START,
    AuditResult,
    CriterionResult,
    ParseError,
    _detect_closure_recommendation,
    _extract_section,
    _parse_criteria_table,
    extract_report,
    parse_audit_output,
)


# ---------------------------------------------------------------------------
# Fixtures — representative audit skill outputs
# ---------------------------------------------------------------------------

AUDIT_PASS_OUTPUT = f"""\
Some preamble noise from opencode...

{AUDIT_REPORT_START}
## Summary

All 5 acceptance criteria are met. The implementation is complete, well-tested,
and follows project conventions.

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | Handler validates from-state | met | ampa/audit/handlers.py:45 |
| 2 | Pre-invariants evaluated | met | ampa/audit/handlers.py:52 |
| 3 | State transition applied | met | ampa/audit/handlers.py:68 |
| 4 | Comment posted | met | ampa/audit/handlers.py:75 |
| 5 | Discord notification sent | met | ampa/audit/handlers.py:82 |

## Children Status

No direct children.

## Recommendation

Can this item be closed? **Yes**. All acceptance criteria are satisfied and
the implementation is production-ready.
{AUDIT_REPORT_END}

More trailing noise...
"""

AUDIT_FAIL_OUTPUT = f"""\
{AUDIT_REPORT_START}
## Summary

2 of 5 acceptance criteria are unmet. The handler does not validate from-state
and Discord notifications are missing.

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | Handler validates from-state | unmet | No from-state check found |
| 2 | Pre-invariants evaluated | met | ampa/audit/handlers.py:52 |
| 3 | State transition applied | met | ampa/audit/handlers.py:68 |
| 4 | Comment posted | met | ampa/audit/handlers.py:75 |
| 5 | Discord notification sent | unmet | No notification code found |

## Recommendation

Can this item be closed? **No**. 2 acceptance criteria remain unmet:
from-state validation and Discord notification integration.
{AUDIT_REPORT_END}
"""

AUDIT_NO_MARKERS_OUTPUT = """\
## Summary

This is an audit without markers. All criteria met.

## Recommendation

Can this item be closed? Yes.
"""

AUDIT_MULTI_AC_OUTPUT = f"""\
{AUDIT_REPORT_START}
## Summary

Mixed results: 8 of 11 acceptance criteria are met.

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | AuditResult dataclass exists | met | ampa/audit/result.py:44 |
| 2 | Parser extracts structured data | met | ampa/audit/result.py:250 |
| 3 | Parser returns ParseError | met | ampa/audit/result.py:96 |
| 4 | Dataclass documented | met | docstrings present |
| 5 | audit_result handler implemented | met | ampa/audit/handlers.py:50 |
| 6 | audit_fail handler implemented | met | ampa/audit/handlers.py:120 |
| 7 | close_with_audit handler | met | ampa/audit/handlers.py:180 |
| 8 | Structured output format | met | See report markers |
| 9 | Discord integration | unmet | No notification calls found |
| 10 | Legacy triage runner removed | unmet | File still exists |
| 11 | Tests refactored | partial | Some tests still reference old API |

## Recommendation

Can this item be closed? No. 2 criteria unmet and 1 partial.
{AUDIT_REPORT_END}
"""


# ---------------------------------------------------------------------------
# Tests: extract_report
# ---------------------------------------------------------------------------


class TestExtractReport:
    def test_extracts_between_markers(self):
        report = extract_report(AUDIT_PASS_OUTPUT)
        assert report.startswith("## Summary")
        assert AUDIT_REPORT_START not in report
        assert AUDIT_REPORT_END not in report

    def test_missing_start_marker_returns_full_text(self):
        text = "No markers here"
        report = extract_report(text)
        assert report == text

    def test_missing_end_marker_returns_after_start(self):
        text = f"preamble\n{AUDIT_REPORT_START}\n## Summary\nHello"
        report = extract_report(text)
        assert "## Summary" in report
        assert "Hello" in report

    def test_empty_input(self):
        assert extract_report("") == ""

    def test_empty_content_between_markers(self):
        text = f"{AUDIT_REPORT_START}\n\n{AUDIT_REPORT_END}"
        report = extract_report(text)
        # Falls back to full text since extracted is empty
        assert AUDIT_REPORT_START in report

    def test_multiple_marker_pairs_uses_first(self):
        text = (
            f"{AUDIT_REPORT_START}\nFirst report\n{AUDIT_REPORT_END}\n"
            f"{AUDIT_REPORT_START}\nSecond report\n{AUDIT_REPORT_END}"
        )
        report = extract_report(text)
        assert report == "First report"

    def test_whitespace_only_content(self):
        text = f"{AUDIT_REPORT_START}\n   \n  \n{AUDIT_REPORT_END}"
        report = extract_report(text)
        # Falls back since stripped content is empty
        assert AUDIT_REPORT_START in report


# ---------------------------------------------------------------------------
# Tests: _extract_section
# ---------------------------------------------------------------------------


class TestExtractSection:
    def test_extracts_summary(self):
        report = extract_report(AUDIT_PASS_OUTPUT)
        summary = _extract_section(report, "Summary")
        assert "acceptance criteria are met" in summary

    def test_extracts_recommendation(self):
        report = extract_report(AUDIT_PASS_OUTPUT)
        rec = _extract_section(report, "Recommendation")
        assert "closed" in rec.lower()

    def test_missing_section_returns_empty(self):
        assert _extract_section("## Summary\nHello", "Missing") == ""

    def test_empty_input(self):
        assert _extract_section("", "Summary") == ""

    def test_section_at_end(self):
        text = "## First\nA\n## Summary\nB"
        assert _extract_section(text, "Summary") == "B"


# ---------------------------------------------------------------------------
# Tests: _parse_criteria_table
# ---------------------------------------------------------------------------


class TestParseCriteriaTable:
    def test_parses_pass_criteria(self):
        report = extract_report(AUDIT_PASS_OUTPUT)
        section = _extract_section(report, "Acceptance Criteria Status")
        criteria = _parse_criteria_table(section)
        assert len(criteria) == 5
        assert all(c.verdict == "met" for c in criteria)

    def test_parses_fail_criteria(self):
        report = extract_report(AUDIT_FAIL_OUTPUT)
        section = _extract_section(report, "Acceptance Criteria Status")
        criteria = _parse_criteria_table(section)
        assert len(criteria) == 5
        unmet = [c for c in criteria if c.verdict == "unmet"]
        assert len(unmet) == 2

    def test_parses_mixed_criteria(self):
        report = extract_report(AUDIT_MULTI_AC_OUTPUT)
        section = _extract_section(report, "Acceptance Criteria Status")
        criteria = _parse_criteria_table(section)
        assert len(criteria) == 11
        met = [c for c in criteria if c.verdict == "met"]
        unmet = [c for c in criteria if c.verdict == "unmet"]
        partial = [c for c in criteria if c.verdict == "partial"]
        assert len(met) == 8
        assert len(unmet) == 2
        assert len(partial) == 1  # criterion 11 only

    def test_empty_section(self):
        assert _parse_criteria_table("") == []

    def test_header_row_skipped(self):
        section = "| # | Criterion | Verdict | Evidence |\n|---|---|---|---|"
        criteria = _parse_criteria_table(section)
        assert len(criteria) == 0


# ---------------------------------------------------------------------------
# Tests: _detect_closure_recommendation
# ---------------------------------------------------------------------------


class TestDetectClosureRecommendation:
    def test_recommends_closure_yes(self):
        report = extract_report(AUDIT_PASS_OUTPUT)
        assert _detect_closure_recommendation(report) is True

    def test_recommends_closure_no(self):
        report = extract_report(AUDIT_FAIL_OUTPUT)
        assert _detect_closure_recommendation(report) is False

    def test_all_criteria_met_implies_closure(self):
        text = (
            "## Summary\nAll good.\n"
            "## Acceptance Criteria Status\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | Thing | met | file.py:1 |\n"
            "## Recommendation\nLooks good."
        )
        assert _detect_closure_recommendation(text) is True

    def test_no_recommendation_section(self):
        text = "## Summary\nNo recommendation section here."
        assert _detect_closure_recommendation(text) is False


# ---------------------------------------------------------------------------
# Tests: parse_audit_output (integration)
# ---------------------------------------------------------------------------


class TestParseAuditOutput:
    def test_pass_recommendation(self):
        result = parse_audit_output(AUDIT_PASS_OUTPUT)
        assert isinstance(result, AuditResult)
        assert result.recommends_closure is True
        assert len(result.acceptance_criteria) == 5
        assert all(c.verdict == "met" for c in result.acceptance_criteria)
        assert "acceptance criteria are met" in result.summary
        assert result.raw_output == AUDIT_PASS_OUTPUT
        assert result.closure_reason  # non-empty

    def test_fail_recommendation(self):
        result = parse_audit_output(AUDIT_FAIL_OUTPUT)
        assert isinstance(result, AuditResult)
        assert result.recommends_closure is False
        assert len(result.acceptance_criteria) == 5
        unmet = [c for c in result.acceptance_criteria if c.verdict == "unmet"]
        assert len(unmet) == 2

    def test_malformed_no_markers(self):
        result = parse_audit_output(AUDIT_NO_MARKERS_OUTPUT)
        assert isinstance(result, AuditResult)
        # Falls back to full text — should still extract summary
        assert result.summary  # non-empty
        assert result.recommends_closure is True

    def test_empty_output(self):
        result = parse_audit_output("")
        assert isinstance(result, ParseError)
        assert "empty" in result.reason.lower()

    def test_whitespace_only_output(self):
        result = parse_audit_output("   \n  \n  ")
        assert isinstance(result, ParseError)
        assert result.raw_output == "   \n  \n  "

    def test_multi_ac_evaluation(self):
        result = parse_audit_output(AUDIT_MULTI_AC_OUTPUT)
        assert isinstance(result, AuditResult)
        assert result.recommends_closure is False
        assert len(result.acceptance_criteria) == 11
        met_count = sum(1 for c in result.acceptance_criteria if c.verdict == "met")
        assert met_count == 8

    def test_preserves_raw_output(self):
        result = parse_audit_output(AUDIT_PASS_OUTPUT)
        assert isinstance(result, AuditResult)
        assert result.raw_output == AUDIT_PASS_OUTPUT

    def test_report_text_excludes_noise(self):
        result = parse_audit_output(AUDIT_PASS_OUTPUT)
        assert isinstance(result, AuditResult)
        assert "preamble noise" not in result.report_text
        assert "trailing noise" not in result.report_text


# ---------------------------------------------------------------------------
# Tests: CriterionResult fields
# ---------------------------------------------------------------------------


class TestCriterionResult:
    def test_fields(self):
        cr = CriterionResult(
            number="1",
            criterion="Test criterion",
            verdict="met",
            evidence="file.py:10",
        )
        assert cr.number == "1"
        assert cr.criterion == "Test criterion"
        assert cr.verdict == "met"
        assert cr.evidence == "file.py:10"

    def test_default_evidence(self):
        cr = CriterionResult(number="1", criterion="Test", verdict="unmet")
        assert cr.evidence == ""


# ---------------------------------------------------------------------------
# Tests: AuditResult fields
# ---------------------------------------------------------------------------


class TestAuditResultFields:
    def test_defaults(self):
        ar = AuditResult(summary="Test")
        assert ar.acceptance_criteria == ()
        assert ar.recommends_closure is False
        assert ar.raw_output == ""
        assert ar.report_text == ""
        assert ar.closure_reason == ""

    def test_frozen(self):
        ar = AuditResult(summary="Test")
        with pytest.raises(AttributeError):
            ar.summary = "Changed"  # type: ignore[misc]
