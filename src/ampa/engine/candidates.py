"""Candidate selection — descriptor-driven filtering, ranking, and rejection tracking.

Extends the existing ``WLNextClient`` and ``normalize_candidates`` from
``ampa.selection`` with workflow-descriptor-aware filtering:

- **Do-not-delegate tag filtering**: Rejects candidates tagged ``do-not-delegate``
  or ``do_not_delegate`` (matching the ``not_do_not_delegate`` invariant).
- **From-state filtering**: Rejects candidates whose ``(status, stage)`` is not
  in a valid ``from`` state for the ``delegate`` command.
- **Global blocker checking**: Queries ``wl in_progress`` to enforce the
  ``no_in_progress_items`` single-concurrency constraint.
- **Ranked output**: Returns a ``CandidateResult`` with the selected candidate,
  all evaluated candidates, rejection reasons, and global rejections.

Usage::

    from ampa.engine.candidates import CandidateSelector

    selector = CandidateSelector(
        descriptor=descriptor,
        wl_client=client,
        in_progress_querier=querier,
    )
    result = selector.select()
    if result.selected:
        print(f"Selected: {result.selected.id}")
    else:
        print(f"No candidates: {result.global_rejections}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ampa.engine.descriptor import StateTuple, WorkflowDescriptor

LOG = logging.getLogger("ampa.engine.candidates")


# ---------------------------------------------------------------------------
# Protocols for external dependencies (mockable)
# ---------------------------------------------------------------------------


class CandidateFetcher(Protocol):
    """Protocol for fetching raw candidate data from ``wl next``."""

    def fetch(self) -> list[dict[str, Any]]:
        """Return a list of raw work item dicts from ``wl next``.

        Returns an empty list if no candidates are available.
        """
        ...


class InProgressQuerier(Protocol):
    """Protocol for checking in-progress work items (``wl in_progress``)."""

    def count_in_progress(self) -> int:
        """Return the count of work items with status ``in_progress``."""
        ...


class NullInProgressQuerier:
    """Default querier that returns 0 (no in-progress items)."""

    def count_in_progress(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItemCandidate:
    """A work item candidate extracted from ``wl next`` output."""

    id: str
    title: str
    status: str
    stage: str
    priority: str = ""
    tags: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class CandidateRejection:
    """A candidate that was rejected with a reason."""

    candidate: WorkItemCandidate
    reason: str


@dataclass(frozen=True)
class CandidateResult:
    """Result of candidate selection.

    Attributes
    ----------
    selected:
        The top candidate that passed all filters, or ``None``.
    candidates:
        All candidates that were evaluated (both accepted and rejected).
    rejections:
        Candidates that were rejected with specific reasons.
    global_rejections:
        Reasons that block *all* delegation (e.g. in-progress items exist).
    """

    selected: WorkItemCandidate | None
    candidates: tuple[WorkItemCandidate, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()
    global_rejections: tuple[str, ...] = ()

    @property
    def has_candidates(self) -> bool:
        """Whether any candidates were returned by ``wl next``."""
        return len(self.candidates) > 0

    def summary(self) -> str:
        """Human-readable summary for Discord notifications."""
        if self.selected:
            return (
                f"Selected candidate: {self.selected.id} "
                f"({self.selected.title}) "
                f"[stage={self.selected.stage}]"
            )
        parts: list[str] = []
        if self.global_rejections:
            parts.append("Global blockers:")
            for gr in self.global_rejections:
                parts.append(f"  - {gr}")
        if self.rejections:
            parts.append("Rejected candidates:")
            for rej in self.rejections:
                parts.append(
                    f"  - {rej.candidate.id} ({rej.candidate.title}): {rej.reason}"
                )
        if not parts:
            parts.append("No candidates available.")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Candidate extraction helpers
# ---------------------------------------------------------------------------


def _extract_id(raw: dict[str, Any]) -> str:
    """Extract work item ID from a raw candidate dict."""
    return str(raw.get("id") or raw.get("work_item_id") or raw.get("workItemId") or "")


def _extract_title(raw: dict[str, Any]) -> str:
    """Extract work item title from a raw candidate dict."""
    return str(raw.get("title") or raw.get("name") or "")


def _extract_status(raw: dict[str, Any]) -> str:
    """Extract work item status from a raw candidate dict."""
    val = raw.get("status") or ""
    return str(val).strip().lower()


def _extract_stage(raw: dict[str, Any]) -> str:
    """Extract work item stage from a raw candidate dict."""
    val = raw.get("stage") or raw.get("state") or ""
    return str(val).strip().lower()


def _extract_priority(raw: dict[str, Any]) -> str:
    """Extract work item priority from a raw candidate dict."""
    return str(raw.get("priority") or "")


def _extract_tags(raw: dict[str, Any]) -> tuple[str, ...]:
    """Extract and normalize tags from a raw candidate dict."""
    tags = raw.get("tags") or raw.get("tag") or []
    if isinstance(tags, str):
        return tuple(t.strip().lower() for t in tags.split(",") if t.strip())
    if isinstance(tags, list):
        return tuple(str(t).strip().lower() for t in tags if t)
    return ()


def to_candidate(raw: dict[str, Any]) -> WorkItemCandidate:
    """Convert a raw ``wl next`` dict into a ``WorkItemCandidate``."""
    return WorkItemCandidate(
        id=_extract_id(raw),
        title=_extract_title(raw),
        status=_extract_status(raw),
        stage=_extract_stage(raw),
        priority=_extract_priority(raw),
        tags=_extract_tags(raw),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Filtering functions (pure, testable)
# ---------------------------------------------------------------------------

# Tags that mark a candidate as not delegatable
DO_NOT_DELEGATE_TAGS = frozenset({"do-not-delegate", "do_not_delegate"})


def is_do_not_delegate(candidate: WorkItemCandidate) -> bool:
    """Check if a candidate is tagged do-not-delegate.

    Checks both tag formats: ``do-not-delegate`` and ``do_not_delegate``.
    Also checks metadata fields for backward compatibility with the legacy
    ``_is_do_not_delegate()`` in scheduler.py.
    """
    # Check normalized tags
    for tag in candidate.tags:
        if tag in DO_NOT_DELEGATE_TAGS:
            return True

    # Check metadata keys (backward compat with scheduler.py)
    raw = candidate.raw
    meta = raw.get("metadata") or raw.get("meta") or {}
    if isinstance(meta, dict):
        for key in ("do_not_delegate", "no_delegation"):
            if str(meta.get(key, "")).strip().lower() in ("1", "true", "yes", "y"):
                return True

    # Explicit field support
    if raw.get("do_not_delegate") in (True, "true", "1", 1):
        return True

    return False


def get_valid_from_states(
    descriptor: WorkflowDescriptor,
    command_name: str = "delegate",
) -> list[StateTuple]:
    """Get the resolved ``from`` states for a command.

    Returns a list of ``StateTuple`` objects representing valid source states.
    """
    cmd = descriptor.get_command(command_name)
    return [descriptor.resolve_state_ref(ref) for ref in cmd.from_states]


def is_in_valid_from_state(
    candidate: WorkItemCandidate,
    valid_states: Sequence[StateTuple],
) -> bool:
    """Check if a candidate's (status, stage) is in the valid from states."""
    candidate_state = StateTuple(
        status=candidate.status,
        stage=candidate.stage,
    )
    return candidate_state in valid_states


# ---------------------------------------------------------------------------
# Selector class
# ---------------------------------------------------------------------------


class CandidateSelector:
    """Descriptor-driven candidate selection with filtering and ranking.

    Parameters
    ----------
    descriptor:
        The loaded workflow descriptor.
    fetcher:
        Fetches raw candidates from ``wl next``.
    in_progress_querier:
        Queries in-progress item count for the global blocker check.
    command_name:
        The command to check ``from`` states against (default: ``delegate``).
    """

    def __init__(
        self,
        descriptor: WorkflowDescriptor,
        fetcher: CandidateFetcher,
        in_progress_querier: InProgressQuerier | None = None,
        command_name: str = "delegate",
    ) -> None:
        self._descriptor = descriptor
        self._fetcher = fetcher
        self._querier = in_progress_querier or NullInProgressQuerier()
        self._command_name = command_name
        self._valid_states = get_valid_from_states(descriptor, command_name)

    def select(self) -> CandidateResult:
        """Run candidate selection with filtering.

        Steps:
        1. Check global blocker (in-progress items exist → reject all).
        2. Fetch candidates from ``wl next``.
        3. Filter each candidate: do-not-delegate tag, valid from state.
        4. Return the first passing candidate as ``selected``.

        Returns
        -------
        CandidateResult
            Complete audit trail of the selection process.
        """
        global_rejections: list[str] = []

        # Step 1: Global blocker — no_in_progress_items
        try:
            in_progress_count = self._querier.count_in_progress()
        except Exception:
            LOG.exception("Failed to query in-progress items")
            return CandidateResult(
                selected=None,
                global_rejections=("Failed to check in-progress items (query error)",),
            )

        if in_progress_count > 0:
            global_rejections.append(
                f"In-progress items exist ({in_progress_count} item(s)): "
                f"single-concurrency constraint blocks delegation"
            )
            # Even with global blockers, still fetch candidates for audit trail
            # but don't select any
            raw_candidates = self._fetch_candidates()
            blocked_candidates = tuple(to_candidate(raw) for raw in raw_candidates)
            return CandidateResult(
                selected=None,
                candidates=blocked_candidates,
                global_rejections=tuple(global_rejections),
            )

        # Step 2: Fetch candidates
        raw_candidates = self._fetch_candidates()
        if not raw_candidates:
            return CandidateResult(
                selected=None,
                global_rejections=("No candidates returned by wl next",),
            )

        # Step 3: Filter candidates
        candidates: list[WorkItemCandidate] = []
        rejections: list[CandidateRejection] = []
        selected: WorkItemCandidate | None = None

        for raw in raw_candidates:
            candidate = to_candidate(raw)
            candidates.append(candidate)

            if not candidate.id:
                rejections.append(
                    CandidateRejection(candidate=candidate, reason="missing id")
                )
                continue

            # Do-not-delegate check
            if is_do_not_delegate(candidate):
                rejections.append(
                    CandidateRejection(
                        candidate=candidate,
                        reason="tagged do-not-delegate",
                    )
                )
                LOG.info(
                    "Candidate %s (%s) rejected: tagged do-not-delegate",
                    candidate.id,
                    candidate.title,
                )
                continue

            # From-state check
            if not is_in_valid_from_state(candidate, self._valid_states):
                rejections.append(
                    CandidateRejection(
                        candidate=candidate,
                        reason=f"stage '{candidate.stage}' is not delegatable "
                        f"(valid stages: {[s.stage for s in self._valid_states]})",
                    )
                )
                LOG.info(
                    "Candidate %s (%s) rejected: stage '%s' not in valid from states",
                    candidate.id,
                    candidate.title,
                    candidate.stage,
                )
                continue

            # Candidate passed all filters
            if selected is None:
                selected = candidate
                LOG.debug(
                    "Candidate selected: %s (%s) [stage=%s]",
                    candidate.id,
                    candidate.title,
                    candidate.stage,
                )

        return CandidateResult(
            selected=selected,
            candidates=tuple(candidates),
            rejections=tuple(rejections),
            global_rejections=tuple(global_rejections),
        )

    def _fetch_candidates(self) -> list[dict[str, Any]]:
        """Fetch and return raw candidate dicts."""
        try:
            return self._fetcher.fetch()
        except Exception:
            LOG.exception("Failed to fetch candidates")
            return []
