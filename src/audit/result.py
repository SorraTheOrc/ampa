"""Structured audit result dataclass and output parser.

Defines the ``AuditResult`` dataclass — the contract between the audit
skill output and the descriptor-driven audit command handlers.  The
    parser extracts structured data from the audit skill's marker-delimited
    output (``--- AUDIT REPORT START/END ---``), replacing the ad-hoc regex
    parsing previously embedded in the legacy monolithic runner. The
    current descriptor-driven audit handlers are the canonical implementation.

Usage::

    from ampa.audit.result import parse_audit_output

    result = parse_audit_output(raw_opencode_output)
    if isinstance(result, AuditResult):
        print(result.summary)
        print(result.recommends_closure)
    else:
        print(f"Parse error: {result.reason}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

LOG = logging.getLogger("ampa.audit.result")

# ---------------------------------------------------------------------------
# Constants — shared with skill/audit/SKILL.md
# ---------------------------------------------------------------------------

AUDIT_REPORT_START = "--- AUDIT REPORT START ---"
AUDIT_REPORT_END = "--- AUDIT REPORT END ---"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionResult:
    """Evaluation result for a single acceptance criterion.

    Attributes:
        number: Criterion number (1-based) or identifier string.
        criterion: The acceptance criterion text.
        verdict: One of ``"met"``, ``"unmet"``, ``"partial"``.
        evidence: Supporting evidence (file:line references, etc.).
    """

    number: str
    criterion: str
    verdict: str
    evidence: str = ""


@dataclass(frozen=True)
class AuditResult:
    """Structured representation of an audit skill output.

    This is the contract between the audit skill
    (``skill/audit/SKILL.md``) and the descriptor-driven audit command
    handlers.  The skill produces marker-delimited markdown output; this
    dataclass captures the parsed, typed representation.

    Attributes:
        summary: 2-4 sentence summary of the audit findings.
        acceptance_criteria: Per-criterion evaluation results.
        recommends_closure: Whether the audit recommends the work item
            can be closed (all acceptance criteria met).
        raw_output: The full raw output from ``opencode run "/audit {id}"``.
        report_text: The extracted report text (between markers).
        closure_reason: Human-readable reason for the closure
            recommendation (from the ``## Recommendation`` section).
    """

    summary: str
    acceptance_criteria: tuple[CriterionResult, ...] = ()
    recommends_closure: bool = False
    raw_output: str = ""
    report_text: str = ""
    closure_reason: str = ""


@dataclass(frozen=True)
class ParseError:
    """Returned when audit output cannot be parsed into an ``AuditResult``.

    Not an exception — callers should check ``isinstance(result, ParseError)``
    and handle the error case structurally.

    Attributes:
        reason: Human-readable description of the parse failure.
        raw_output: The raw output that could not be parsed.
    """

    reason: str
    raw_output: str = ""


# Type alias for parse results
AuditParseResult = Union[AuditResult, ParseError]


# ---------------------------------------------------------------------------
# Report extraction
# ---------------------------------------------------------------------------


def extract_report(text: str) -> str:
    """Extract the structured audit report from raw audit output.

    Looks for ``--- AUDIT REPORT START ---`` and
    ``--- AUDIT REPORT END ---`` delimiter lines.  Returns the content
    between these markers (stripped of leading/trailing whitespace).

    Fallback behavior:

    - If the start marker is missing the full *text* is returned with a
      warning.
    - If the start marker is present but the end marker is missing, all
      content after the start marker is returned (with a warning).
    - If the extracted content is empty, the full *text* is returned
      with a warning.
    - When multiple marker pairs exist only the **first** pair is used.
    """
    if not text:
        return ""

    start_idx = text.find(AUDIT_REPORT_START)
    if start_idx == -1:
        LOG.warning(
            "Audit output missing start marker (%s); using full output",
            AUDIT_REPORT_START,
        )
        return text

    content_start = start_idx + len(AUDIT_REPORT_START)
    end_idx = text.find(AUDIT_REPORT_END, content_start)
    if end_idx == -1:
        LOG.warning(
            "Audit output missing end marker (%s); using content after start marker",
            AUDIT_REPORT_END,
        )
        extracted = text[content_start:].strip()
    else:
        extracted = text[content_start:end_idx].strip()

    if not extracted:
        LOG.warning("Extracted audit report is empty; falling back to full output")
        return text

    return extracted


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


def _extract_section(report: str, heading: str) -> str:
    """Extract a ``## <heading>`` section from a markdown report.

    Returns the text between the heading and the next ``##`` heading (or
    end of string), stripped.  Returns ``""`` if not found.
    """
    if not report:
        return ""
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    m = re.search(pattern, report, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    m2 = re.search(r"^##\s+", report[start:], re.MULTILINE)
    if m2:
        section = report[start : start + m2.start()]
    else:
        section = report[start:]
    return section.strip()


# ---------------------------------------------------------------------------
# Criteria table parsing
# ---------------------------------------------------------------------------

# Match rows like: | 1 | criterion text | met | evidence |
# or: | SC-1 | criterion text | unmet | evidence |
_CRITERIA_ROW_RE = re.compile(
    r"^\|\s*(?P<num>[^|]+?)\s*\|\s*(?P<crit>[^|]+?)\s*\|\s*(?P<verdict>[^|]+?)\s*\|\s*(?P<evidence>[^|]*?)\s*\|",
    re.MULTILINE,
)

# Separator row: | --- | --- | --- | --- |
_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|[\s\-:|]+\|", re.MULTILINE)


def _parse_criteria_table(section: str) -> list[CriterionResult]:
    """Parse acceptance criteria from a markdown table.

    Expects rows in the format:
    ``| # | Criterion | Verdict | Evidence |``

    Skips header rows and separator rows.
    """
    results: List[CriterionResult] = []
    for m in _CRITERIA_ROW_RE.finditer(section):
        num = m.group("num").strip()
        crit = m.group("crit").strip()
        verdict = m.group("verdict").strip().lower()
        evidence = m.group("evidence").strip()

        # Skip header rows
        if num.lower() in ("#", "no", "no.", "number", "id"):
            continue
        # Skip separator rows (all dashes/colons/spaces)
        if all(c in "-: " for c in num):
            continue

        # Normalize verdict
        if verdict in ("met", "pass", "passed", "yes", "true", "done"):
            verdict = "met"
        elif verdict in ("unmet", "fail", "failed", "no", "false", "missing"):
            verdict = "unmet"
        elif verdict in ("partial", "partially", "incomplete"):
            verdict = "partial"

        results.append(
            CriterionResult(
                number=num,
                criterion=crit,
                verdict=verdict,
                evidence=evidence,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Closure recommendation detection
# ---------------------------------------------------------------------------

# From workflow.yaml invariants:
#   audit_recommends_closure: regex(comments, "(?i)(can this item be closed?\s*yes|...)")
#   audit_does_not_recommend_closure: regex(comments, "(?i)can this item be closed?\s*no")
_RECOMMENDS_CLOSURE_RE = re.compile(
    r"(?i)(can this item be closed\?\s*yes|"
    r"all acceptance criteria.*(met|satisfied))",
)
_DOES_NOT_RECOMMEND_RE = re.compile(
    r"(?i)can this item be closed\?\s*no",
)


def _detect_closure_recommendation(report: str) -> bool:
    """Determine whether the audit recommends closure.

    Checks the ``## Recommendation`` section first for explicit yes/no
    signals, then falls back to the full report.  If neither contains an
    explicit signal, checks whether all acceptance criteria are met.
    Returns ``True`` if a closure recommendation is detected, ``False``
    otherwise.
    """
    recommendation = _extract_section(report, "Recommendation")

    # 1. Check recommendation section for explicit signals (if present)
    if recommendation:
        if _RECOMMENDS_CLOSURE_RE.search(recommendation):
            return True
        if _DOES_NOT_RECOMMEND_RE.search(recommendation):
            return False

    # 2. Check full report for explicit signals
    if _RECOMMENDS_CLOSURE_RE.search(report):
        return True
    if _DOES_NOT_RECOMMEND_RE.search(report):
        return False

    # 3. Fallback: infer from criteria — all met implies closure
    criteria_section = _extract_section(report, "Acceptance Criteria Status")
    if criteria_section:
        criteria = _parse_criteria_table(criteria_section)
        if criteria and all(c.verdict == "met" for c in criteria):
            return True

    return False


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_audit_output(raw_output: str) -> AuditParseResult:
    """Parse raw ``opencode run "/audit {id}"`` output into an ``AuditResult``.

    Extracts the structured report from between the markers, then parses
    the ``## Summary``, ``## Acceptance Criteria Status``, and
    ``## Recommendation`` sections.

    Returns an ``AuditResult`` on success or a ``ParseError`` if the
    output is empty or contains no recognizable audit content.

    This function never raises — all parse failures are returned as
    ``ParseError`` instances.
    """
    if not raw_output or not raw_output.strip():
        return ParseError(
            reason="Empty audit output",
            raw_output=raw_output or "",
        )

    try:
        report = extract_report(raw_output)

        if not report or not report.strip():
            return ParseError(
                reason="No audit report content found after marker extraction",
                raw_output=raw_output,
            )

        # Extract sections
        summary = _extract_section(report, "Summary")
        criteria_section = _extract_section(report, "Acceptance Criteria Status")
        criteria = _parse_criteria_table(criteria_section) if criteria_section else []
        closure_reason = _extract_section(report, "Recommendation")
        recommends_closure = _detect_closure_recommendation(report)

        # If we have no summary, try to extract something meaningful
        if not summary:
            # Use first non-heading paragraph as summary
            lines = report.strip().splitlines()
            summary_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#"):
                    if summary_lines:
                        break
                    continue
                if stripped.startswith("|"):
                    if summary_lines:
                        break
                    continue
                if stripped:
                    summary_lines.append(stripped)
                elif summary_lines:
                    break
            summary = " ".join(summary_lines) if summary_lines else ""

        return AuditResult(
            summary=summary,
            acceptance_criteria=tuple(criteria),
            recommends_closure=recommends_closure,
            raw_output=raw_output,
            report_text=report,
            closure_reason=closure_reason,
        )

    except Exception as exc:
        LOG.exception("Unexpected error parsing audit output")
        return ParseError(
            reason=f"Unexpected parse error: {exc}",
            raw_output=raw_output,
        )
