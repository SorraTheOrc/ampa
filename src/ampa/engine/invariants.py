"""Invariant evaluator — evaluates pre/post invariants from the workflow descriptor.

Evaluates invariant logic expressions against work item data to enforce
preconditions and postconditions for workflow commands.

Supported expression patterns (from workflow.yaml)::

    length(description) > N          — string length check
    regex(field, "pattern")          — regex match against a field
    stage in ["a", "b", "c"]         — membership check
    "value" not in tags              — tag exclusion
    X and Y                          — boolean AND
    count(work_items, status="X") == N — count items (requires external call)

Usage::

    from ampa.engine.invariants import InvariantEvaluator

    evaluator = InvariantEvaluator(descriptor.invariants)
    result = evaluator.evaluate(["not_do_not_delegate"], work_item_data)
    if not result.passed:
        print(result.summary())
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from ampa.engine.descriptor import Invariant


# ---------------------------------------------------------------------------
# Protocols / interfaces for external dependencies
# ---------------------------------------------------------------------------


class WorkItemQuerier(Protocol):
    """Protocol for querying work item state from external sources.

    Implementations call ``wl in_progress --json`` or equivalent.
    """

    def count_in_progress(self) -> int:
        """Return the count of work items currently in_progress."""
        ...


class NullQuerier:
    """Default querier that always returns 0 (no external calls)."""

    def count_in_progress(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingleInvariantResult:
    """Result of evaluating a single invariant."""

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class InvariantResult:
    """Aggregate result of evaluating one or more invariants."""

    passed: bool
    results: tuple[SingleInvariantResult, ...]

    @property
    def failed_invariants(self) -> list[str]:
        """Names of invariants that failed."""
        return [r.name for r in self.results if not r.passed]

    def summary(self) -> str:
        """Human-readable summary suitable for Discord notifications."""
        if self.passed:
            return f"All {len(self.results)} invariant(s) passed."
        failed = [r for r in self.results if not r.passed]
        lines = [f"{len(failed)} of {len(self.results)} invariant(s) failed:"]
        for r in failed:
            lines.append(f"  - {r.name}: {r.reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Work item data adapter
# ---------------------------------------------------------------------------


def extract_work_item_fields(work_item: dict[str, Any]) -> dict[str, Any]:
    """Extract evaluatable fields from ``wl show`` JSON output.

    Returns a normalized dict with keys:
        description, comments_text, tags, status, stage, assignee,
        title, priority, raw (original dict).
    """
    # Handle nested workItem wrapper from wl show --json
    wi = work_item.get("workItem", work_item)

    description = wi.get("description", "")
    tags = wi.get("tags", []) or []

    # Normalize comments into a single searchable string
    comments_list = work_item.get("comments", []) or []
    comments_parts: list[str] = []
    for c in comments_list:
        text = c.get("comment", "") or c.get("body", "") or c.get("text", "")
        if text:
            comments_parts.append(text)
    comments_text = "\n".join(comments_parts)

    return {
        "description": description,
        "comments_text": comments_text,
        "tags": tags,
        "status": wi.get("status", ""),
        "stage": wi.get("stage", ""),
        "assignee": wi.get("assignee", ""),
        "title": wi.get("title", ""),
        "priority": wi.get("priority", ""),
        "raw": wi,
    }


# ---------------------------------------------------------------------------
# Expression evaluator
# ---------------------------------------------------------------------------


def _eval_length(fields: dict[str, Any], field_name: str, op: str, value: int) -> bool:
    """Evaluate ``length(field) > N`` or ``length(field) >= N``."""
    text = str(fields.get(field_name, ""))
    length = len(text)
    if op == ">":
        return length > value
    elif op == ">=":
        return length >= value
    elif op == "<":
        return length < value
    elif op == "<=":
        return length <= value
    elif op == "==":
        return length == value
    return False


def _eval_regex(fields: dict[str, Any], field_name: str, pattern: str) -> bool:
    """Evaluate ``regex(field, "pattern")``.

    The pattern undergoes one level of backslash un-escaping because YAML
    single-quoted strings preserve literal backslashes, resulting in ``\\\\s``
    where the regex intent is ``\\s``.
    """
    # Map field names to actual data
    if field_name == "comments":
        text = fields.get("comments_text", "")
    else:
        text = str(fields.get(field_name, ""))
    # Unescape one level: YAML '\\s' -> regex '\s'
    unescaped = _unescape_regex(pattern)
    try:
        return bool(re.search(unescaped, text))
    except re.error:
        return False


def _unescape_regex(pattern: str) -> str:
    """Remove one level of backslash escaping from a regex pattern.

    Converts ``\\\\s`` to ``\\s``, ``\\\\w`` to ``\\w``, etc.  This is needed
    because YAML single-quoted strings preserve literal backslashes, so
    ``'\\\\s'`` in YAML becomes the two-character sequence ``\\`` + ``s`` in
    Python, but the intended regex metacharacter is ``\\s`` (one backslash + s).
    """
    result: list[str] = []
    i = 0
    while i < len(pattern):
        if i + 1 < len(pattern) and pattern[i] == "\\" and pattern[i + 1] == "\\":
            # Double backslash -> single backslash
            result.append("\\")
            i += 2
        else:
            result.append(pattern[i])
            i += 1
    return "".join(result)


def _eval_membership(
    fields: dict[str, Any], field_name: str, values: list[str]
) -> bool:
    """Evaluate ``field in [values]``."""
    actual = str(fields.get(field_name, ""))
    return actual in values


def _eval_not_in_tags(fields: dict[str, Any], value: str) -> bool:
    """Evaluate ``"value" not in tags``."""
    tags = fields.get("tags", [])
    return value not in tags


def _eval_count(querier: WorkItemQuerier, status: str, op: str, value: int) -> bool:
    """Evaluate ``count(work_items, status="X") == N``."""
    count = querier.count_in_progress()
    if op == "==":
        return count == value
    elif op == ">":
        return count > value
    elif op == ">=":
        return count >= value
    elif op == "<":
        return count < value
    elif op == "<=":
        return count <= value
    elif op == "!=":
        return count != value
    return False


# Regex patterns for parsing invariant logic expressions
_RE_LENGTH = re.compile(r"length\((\w+)\)\s*(>|>=|<|<=|==)\s*(\d+)")
_RE_REGEX = re.compile(
    r"""regex\((\w+),\s*(['"])(.*?)\2\s*\)""",
    re.DOTALL,
)
_RE_STAGE_IN = re.compile(r"(\w+)\s+in\s+\[([^\]]+)\]")
_RE_NOT_IN_TAGS = re.compile(r"""['"]([^'"]+)['"]\s+not\s+in\s+tags""")
_RE_COUNT = re.compile(
    r'count\(work_items,\s*status\s*=\s*["\'](\w+)["\']\)\s*(==|>|>=|<|<=|!=)\s*(\d+)'
)


def evaluate_logic(
    logic: str,
    fields: dict[str, Any],
    querier: WorkItemQuerier,
) -> tuple[bool, str]:
    """Evaluate an invariant logic expression.

    Returns ``(passed, reason)`` where *reason* explains failure.
    """
    if not logic:
        # No logic means the invariant is always satisfied (declarative-only)
        return True, "No logic expression defined (always passes)"

    # Handle compound expressions with " and "
    # Split on " and " but not inside regex patterns
    parts = _split_and_expression(logic)
    if len(parts) > 1:
        for part in parts:
            passed, reason = evaluate_logic(part.strip(), fields, querier)
            if not passed:
                return False, reason
        return True, "All sub-expressions passed"

    expr = logic.strip()

    # length(field) > N
    m = _RE_LENGTH.search(expr)
    if m:
        field_name, op, val = m.group(1), m.group(2), int(m.group(3))
        actual = len(str(fields.get(field_name, "")))
        passed = _eval_length(fields, field_name, op, int(val))
        if not passed:
            return False, f"length({field_name}) is {actual}, expected {op} {val}"
        return True, f"length({field_name}) = {actual} {op} {val}"

    # regex(field, "pattern")
    m = _RE_REGEX.search(expr)
    if m:
        field_name, _, pattern = m.group(1), m.group(2), m.group(3)
        passed = _eval_regex(fields, field_name, pattern)
        if not passed:
            return False, f"regex({field_name}, ...) did not match"
        return True, f"regex({field_name}, ...) matched"

    # field in [values]
    m = _RE_STAGE_IN.search(expr)
    if m:
        field_name = m.group(1)
        values_str = m.group(2)
        values = [v.strip().strip('"').strip("'") for v in values_str.split(",")]
        passed = _eval_membership(fields, field_name, values)
        if not passed:
            actual = fields.get(field_name, "")
            return False, f"{field_name} is '{actual}', expected one of {values}"
        return True, f"{field_name} is in {values}"

    # "value" not in tags
    m = _RE_NOT_IN_TAGS.search(expr)
    if m:
        value = m.group(1)
        passed = _eval_not_in_tags(fields, value)
        if not passed:
            return False, f"Tag '{value}' is present (should not be)"
        return True, f"Tag '{value}' is not present"

    # count(work_items, status="X") == N
    m = _RE_COUNT.search(expr)
    if m:
        status, op, val = m.group(1), m.group(2), int(m.group(3))
        passed = _eval_count(querier, status, op, int(val))
        count = querier.count_in_progress()
        if not passed:
            return (
                False,
                f"count(work_items, status={status}) is {count}, expected {op} {val}",
            )
        return True, f"count(work_items, status={status}) is {count} {op} {val}"

    # Unrecognized expression — fail open with a warning
    return True, f"Unrecognized logic expression (skipped): {expr}"


def _split_and_expression(logic: str) -> list[str]:
    """Split a logic expression on top-level `` and `` connectives.

    Avoids splitting inside parentheses or quoted strings.
    """
    parts: list[str] = []
    depth = 0
    in_quote: str | None = None
    current: list[str] = []
    i = 0

    while i < len(logic):
        ch = logic[i]

        # Track quotes
        if ch in ('"', "'") and (i == 0 or logic[i - 1] != "\\"):
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None

        # Track parentheses (only outside quotes)
        if in_quote is None:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1

        # Check for " and " at depth 0 outside quotes
        if depth == 0 and in_quote is None and i + 5 <= len(logic):
            if logic[i : i + 5] == " and ":
                parts.append("".join(current))
                current = []
                i += 5
                continue

        current.append(ch)
        i += 1

    parts.append("".join(current))
    return parts


# ---------------------------------------------------------------------------
# Evaluator class
# ---------------------------------------------------------------------------


class InvariantEvaluator:
    """Evaluates named invariants against work item data.

    Parameters
    ----------
    invariants:
        Invariant definitions from the workflow descriptor.
    querier:
        Optional implementation for external queries (``wl in_progress``).
        Defaults to ``NullQuerier`` which returns 0 for all counts.
    """

    def __init__(
        self,
        invariants: Sequence[Invariant],
        querier: WorkItemQuerier | None = None,
    ) -> None:
        self._index: dict[str, Invariant] = {inv.name: inv for inv in invariants}
        self._querier = querier or NullQuerier()

    def evaluate(
        self,
        invariant_names: Sequence[str],
        work_item: dict[str, Any],
        *,
        fail_fast: bool = True,
    ) -> InvariantResult:
        """Evaluate the named invariants against *work_item* data.

        Parameters
        ----------
        invariant_names:
            Names of invariants to evaluate.
        work_item:
            Raw ``wl show`` JSON output (may contain ``workItem`` wrapper).
        fail_fast:
            If ``True`` (default), stop on the first failure.
            If ``False``, evaluate all invariants and report all results.

        Returns
        -------
        InvariantResult
            Aggregate result with per-invariant details.

        Raises
        ------
        KeyError
            If an invariant name is not found in the loaded invariants.
        """
        fields = extract_work_item_fields(work_item)
        results: list[SingleInvariantResult] = []
        all_passed = True

        for name in invariant_names:
            if name not in self._index:
                raise KeyError(
                    f"Unknown invariant '{name}'. "
                    f"Known invariants: {', '.join(sorted(self._index))}"
                )
            inv = self._index[name]
            passed, reason = evaluate_logic(inv.logic, fields, self._querier)
            results.append(
                SingleInvariantResult(name=name, passed=passed, reason=reason)
            )
            if not passed:
                all_passed = False
                if fail_fast:
                    break

        return InvariantResult(passed=all_passed, results=tuple(results))
