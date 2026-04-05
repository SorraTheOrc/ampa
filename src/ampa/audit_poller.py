"""Audit detection & polling flow.

This module extracts audit candidate detection, cooldown filtering, and
one-at-a-time selection into a focused polling layer. It queries for
``in_review`` items, applies store-based cooldown, selects the oldest
eligible candidate, and hands it off to the audit command handlers via a
well-defined protocol.

Work item: SA-0MLYEOG9V107HE1D
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

LOG = logging.getLogger("ampa.audit_poller")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class PollerOutcome(enum.Enum):
    """Possible outcomes of a polling cycle."""

    no_candidates = "no_candidates"
    """No work items passed cooldown filtering (or none are in_review)."""

    handed_off = "handed_off"
    """A candidate was selected and handed off to the audit handler."""

    query_failed = "query_failed"
    """The ``wl list`` query failed (non-zero exit code or invalid JSON)."""


@dataclass(frozen=True)
class PollerResult:
    """Structured result returned by the polling cycle.

    Attributes:
        outcome: The outcome of this polling cycle.
        selected_item_id: The work item ID of the selected candidate, or
            ``None`` when no candidate was handed off.
        error: An optional error message when *outcome* is
            ``PollerOutcome.query_failed``.
    """

    outcome: PollerOutcome
    selected_item_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Handoff protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditHandoffHandler(Protocol):
    """Protocol for the audit command handler that receives a selected
    candidate from the poller.

    Implementations must define a ``__call__`` method that accepts a single
    work item dict (the shape returned by ``wl list --json``) and returns
    ``True`` on success or ``False`` on failure.

    The work item dict is expected to contain at least the following keys
    (matching the output of ``wl list --json`` / ``wl show <id> --json``):

    - ``id`` (str): The work item identifier.
    - ``title`` (str): Human-readable title.
    - ``status`` (str): Current status (e.g. ``"in-progress"``).
    - ``stage`` (str): Current stage (e.g. ``"in_review"``).
    - ``priority`` (str): Priority label.
    - ``updatedAt`` or ``updated_at`` (str | None): ISO-8601 timestamp of
      the last update, used for candidate ordering.

    Example usage::

        class MyHandler:
            def __call__(self, work_item: Dict[str, Any]) -> bool:
                # execute audit logic
                return True

        handler: AuditHandoffHandler = MyHandler()
        result = poll_and_handoff(..., handler=handler)
    """

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        """Execute the audit for the given *work_item*.

        Args:
            work_item: A dict representing the work item as returned by
                ``wl list --json``.

        Returns:
            ``True`` if the audit completed successfully, ``False``
            otherwise.  The poller will persist the ``last_audit_at``
            timestamp only when the handler returns ``True``. The
            handler's return value is therefore used to decide whether
            the candidate should be considered successfully audited. The
            poller may also record a per-item ``last_attempt_at`` for
            observability when handlers fail or raise.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Candidate query & normalization
# ---------------------------------------------------------------------------


def _query_candidates(
    run_shell: Callable[..., subprocess.CompletedProcess],
    cwd: str,
    timeout: int = 60,
) -> Optional[List[Dict[str, Any]]]:
    """Query ``wl list --stage in_review --json`` and return normalised items.

    Handles multiple JSON response shapes:

    - A bare list of work item dicts.
    - A dict wrapping the list under ``workItems``, ``work_items``,
      ``items``, ``data``, or any key ending with ``workitems``
      (case-insensitive).

    Deduplicates items by ID (``id``, ``work_item_id``, or ``work_item``
    key).  Items without a recognisable ID are silently dropped.

    This function never raises.  On query failure (non-zero exit code,
    invalid JSON, execution error) it logs the error and returns ``None``
    to signal a query failure (as opposed to an empty list, which means
    the query succeeded but found no ``in_review`` items).

    Args:
        run_shell: Callable that executes a shell command and returns a
            ``subprocess.CompletedProcess`` instance.
        cwd: Working directory for the shell command.
        timeout: Maximum seconds for the shell command.

    Returns:
        A list of unique work item dicts (each guaranteed to have an
        ``"id"`` key) on success, or ``None`` on query failure.
    """
    try:
        proc = run_shell(
            "wl list --stage in_review --json",
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    except Exception:
        LOG.exception("wl list --stage in_review command failed to execute")
        return None

    if proc.returncode != 0:
        LOG.warning(
            "wl list --stage in_review exited with code %s: %s",
            proc.returncode,
            proc.stderr,
        )
        return None

    try:
        raw = json.loads(proc.stdout or "null")
    except Exception:
        LOG.exception("Failed to parse wl list --stage in_review output as JSON")
        return None

    items: List[Dict[str, Any]] = []

    if isinstance(raw, list):
        items.extend(raw)
    elif isinstance(raw, dict):
        for key in ("workItems", "work_items", "items", "data"):
            val = raw.get(key)
            if isinstance(val, list):
                items.extend(val)
                break
        if not items:
            for k, v in raw.items():
                if isinstance(v, list) and k.lower().endswith("workitems"):
                    items.extend(v)
                    break

    # Deduplicate by ID.
    # Prefer the representation with the newer `updated_at` timestamp when
    # duplicates are present. When timestamps are unavailable or equal, fall
    # back to a deterministic tie-breaker (JSON-serialized ordering).
    unique: Dict[str, Dict[str, Any]] = {}
    for it in items:
        # Be defensive: skip non-dict entries that may appear in mixed-type
        # lists returned by upstream providers (e.g. stray strings/nulls).
        if not isinstance(it, dict):
            continue
        wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
        if not wid:
            continue
        wid = str(wid)
        candidate = {**it, "id": wid}

        existing = unique.get(wid)
        if existing is None:
            unique[wid] = candidate
            continue

        # Attempt to compare update timestamps. _item_updated_ts may return
        # None when no valid timestamp is present.
        try:
            existing_ts = _item_updated_ts(existing)
        except Exception:
            existing_ts = None
        try:
            candidate_ts = _item_updated_ts(candidate)
        except Exception:
            candidate_ts = None

        if existing_ts is not None and candidate_ts is not None:
            # Keep the newer (later) timestamp
            if candidate_ts > existing_ts:
                unique[wid] = candidate
        elif existing_ts is None and candidate_ts is None:
            # Deterministic tie-breaker: choose the lexicographically smaller
            # JSON representation (stable across runs).
            existing_serial = json.dumps(existing, sort_keys=True, default=str)
            candidate_serial = json.dumps(candidate, sort_keys=True, default=str)
            unique[wid] = existing if existing_serial <= candidate_serial else candidate
        else:
            # One item has a timestamp while the other does not; prefer the one
            # that includes a timestamp (more informative).
            if candidate_ts is not None:
                unique[wid] = candidate
            else:
                # keep existing
                pass

    return list(unique.values())


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse an ISO-8601 timestamp string, returning ``None`` on failure."""
    if not value:
        return None
    try:
        v = value
        if isinstance(v, str) and v.endswith("Z"):
            v = v[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(v)
        # Coerce naive datetimes to UTC for consistent comparisons.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cooldown filtering
# ---------------------------------------------------------------------------


def _filter_by_cooldown(
    candidates: List[Dict[str, Any]],
    last_audit_by_item: Dict[str, str],
    cooldown_hours: int,
    now: dt.datetime,
) -> List[Dict[str, Any]]:
    """Filter candidates by store-based cooldown.

    For each candidate, look up ``last_audit_at_by_item[item_id]`` from the
    scheduler store state.  If ``(now - last_audit_at) < cooldown_hours`` the
    candidate is skipped.  Items exactly at the cooldown boundary **are**
    eligible (exclusive comparison).  Items with no store entry are always
    eligible.

    Args:
        candidates: Work item dicts as returned by :func:`_query_candidates`.
        last_audit_by_item: Mapping of work item ID to ISO-8601 timestamp
            string, as persisted in the scheduler store under the key
            ``last_audit_at_by_item``.
        cooldown_hours: Minimum hours between audits for the same item.
        now: Current UTC datetime used for comparison.

    Returns:
        A list of candidates that have passed the cooldown check.
    """
    cooldown_delta = dt.timedelta(hours=cooldown_hours)
    eligible: List[Dict[str, Any]] = []

    for item in candidates:
        wid = str(item.get("id", ""))
        if not wid:
            continue

        last_audit_iso = last_audit_by_item.get(wid)
        last_audit = None
        if last_audit_iso:
            last_audit = _from_iso(last_audit_iso)

        # If the item has been updated since the last successful audit,
        # consider it eligible immediately regardless of cooldown. This
        # implements: "audit any item that has been modified since the
        # last time it was audited and is in the in_review stage."  When
        # update timestamps are not available we fall back to the normal
        # cooldown logic.
        try:
            updated_ts = _item_updated_ts(item)
        except Exception:
            updated_ts = None

        if last_audit is not None and updated_ts is not None and updated_ts > last_audit:
            eligible.append(item)
            continue

        if last_audit is not None and (now - last_audit) < cooldown_delta:
            continue

        eligible.append(item)

    return eligible


# ---------------------------------------------------------------------------
# Candidate selection & sorting
# ---------------------------------------------------------------------------


def _item_updated_ts(item: Dict[str, Any]) -> Optional[dt.datetime]:
    """Extract and parse the ``updated_at`` timestamp from a work item dict.

    Tries multiple key variants used by different ``wl`` output formats.
    Returns ``None`` if no valid timestamp is found.
    """
    for key in (
        "updatedAt",
        "updated_at",
        "last_updated_at",
        "updated_ts",
        "updated",
        "last_update_ts",
    ):
        val = item.get(key)
        if val:
            parsed = _from_iso(val)
            if parsed is not None:
                return parsed
            try:
                return dt.datetime.fromisoformat(val)
            except Exception:
                continue
    return None


def _select_candidate(
    candidates: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Sort candidates by ``updated_at`` ascending and return the oldest.

    Items with no ``updated_at`` timestamp are sorted first (most likely to
    be the oldest).  Returns ``None`` when the candidate list is empty.
    """
    if not candidates:
        return None

    # Precompute parsed timestamps once per candidate to avoid reparsing
    # during comparison (performance and determinism improvement).
    epoch = dt.datetime.fromtimestamp(0, dt.timezone.utc)
    augmented: List[tuple[Dict[str, Any], Optional[dt.datetime]]] = []
    for it in candidates:
        try:
            parsed = _item_updated_ts(it)
        except Exception:
            # Be conservative: on parse error treat as missing timestamp
            parsed = None
        augmented.append((it, parsed))

    # Sort by (has_timestamp, timestamp_or_epoch) so that items with no
    # timestamp sort first (considered oldest), then by ascending timestamp.
    augmented.sort(key=lambda pair: (pair[1] is not None, pair[1] or epoch))

    return augmented[0][0]


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def poll_and_handoff(
    run_shell: Callable[..., subprocess.CompletedProcess],
    cwd: str,
    store: Any,
    spec: Any,
    handler: AuditHandoffHandler,
    now: Optional[dt.datetime] = None,
) -> PollerResult:
    """Execute one polling cycle: query, filter, select, persist, handoff.

    1. Query ``wl list --stage in_review --json`` for candidate items.
    2. Filter candidates by store-based cooldown.
    3. Select the oldest eligible candidate.
    4. Hand off the selected candidate to *handler*.
    5. Persist ``last_audit_at`` to the store **only when** the handler
       indicates success (truthy return). The poller always attempts to
       record a per-item ``last_attempt_at`` for observability regardless
       of handler outcome.

    This function never raises.  All errors are caught, logged, and returned
    as a :class:`PollerResult` with the appropriate outcome.

    Args:
        run_shell: Callable that executes a shell command.
        cwd: Working directory for shell commands.
        store: A ``SchedulerStore`` instance with ``get_state()`` and
            ``update_state()`` methods.
        spec: A ``CommandSpec`` instance.  ``spec.command_id`` identifies the
            state key; ``spec.metadata.get("audit_cooldown_hours", 6)``
            provides the cooldown interval.
        handler: An :class:`AuditHandoffHandler` that receives the selected
            work item dict.
        now: Override for the current UTC time (for testing).

    Returns:
        A :class:`PollerResult` describing the outcome.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    # 1. Query candidates
    candidates = _query_candidates(run_shell, cwd)
    if candidates is None:
        # _query_candidates returns None on query failure (non-zero exit,
        # invalid JSON, execution error) to distinguish from an empty list
        # (query succeeded, no in_review items).
        return PollerResult(
            outcome=PollerOutcome.query_failed,
            error="wl list query failed (see logs for details)",
        )

    if not candidates:
        LOG.info("Audit poller: no items in_review")
        return PollerResult(outcome=PollerOutcome.no_candidates)

    # 2. Read cooldown config
    try:
        meta = spec.metadata or {}
    except Exception:
        meta = {}
    try:
        cooldown_hours = int(meta.get("audit_cooldown_hours", 6))
    except Exception:
        cooldown_hours = 6

    # 3. Read persisted state
    try:
        state = store.get_state(spec.command_id)
        if not isinstance(state, dict):
            state = dict(state or {})
    except Exception:
        LOG.exception("Failed to read scheduler store state")
        state = {}

    last_audit_by_item = state.get("last_audit_at_by_item", {})
    if not isinstance(last_audit_by_item, dict):
        last_audit_by_item = {}

    # 4. Filter by cooldown
    eligible = _filter_by_cooldown(candidates, last_audit_by_item, cooldown_hours, now)
    if not eligible:
        LOG.info("Audit poller: no candidates after cooldown filter")
        return PollerResult(outcome=PollerOutcome.no_candidates)

    # 5. Select oldest candidate
    selected = _select_candidate(eligible)
    if selected is None:
        LOG.info("Audit poller: select_candidate returned None")
        return PollerResult(outcome=PollerOutcome.no_candidates)

    work_id = str(selected.get("id", ""))
    if not work_id:
        LOG.warning("Audit poller: selected candidate has no id")
        return PollerResult(outcome=PollerOutcome.no_candidates)

    title = selected.get("title") or selected.get("name") or "(no title)"
    LOG.info("Audit poller: selected candidate %s — %s", work_id, title)

    # 6. Before handing off to the descriptor-driven handler, attempt to
    # re-fetch the full work item so we can validate its current
    # (status, stage).  Some backends return minimal fields from
    # `wl list --json` and the handler will re-fetch later, but that
    # causes noisy "invalid_from_state" warnings when items have a
    # mismatched status (e.g. status=completed with stage=in_review).  To
    # avoid repeatedly selecting such candidates, re-fetch here and skip
    # items whose status is not the expected "in_progress" value.
    #
    # If the fetch fails for any reason we fall through and call the
    # handler (preserving the previous behavior) so operators still see
    # diagnostic output.
    try:
        fetch_cmd = f"wl show {work_id} --children --json"
        proc = run_shell(fetch_cmd, shell=True, check=False, capture_output=True, text=True, cwd=cwd, timeout=60)
        if proc.returncode == 0 and proc.stdout:
            try:
                payload = json.loads(proc.stdout)
                # Work item may be wrapped under 'workItem' — normalize
                wi = payload.get("workItem") or payload
                status_val = (wi.get("status") or "").strip().lower()
                # Normalize common variants to a canonical form
                status_norm = status_val.replace("-", "_").replace(" ", "_")
                if status_norm != "in_progress":
                    LOG.info(
                        "Audit poller: skipping candidate %s due to status=%r (expected in_progress)",
                        work_id,
                        status_val,
                    )
                    # Record an attempt timestamp so we don't repeatedly
                    # re-select this candidate on the next cycle.
                    try:
                        state.setdefault("last_attempt_at_by_item", {})
                        state["last_attempt_at_by_item"][work_id] = now.isoformat()
                        store.update_state(spec.command_id, state)
                    except Exception:
                        LOG.exception("Failed to persist attempt timestamp for skipped candidate %s", work_id)
                    # Remove the selected item from eligible and try again
                    try:
                        eligible.remove(selected)
                    except ValueError:
                        pass
                    # If there are other eligible candidates, recurse (simple
                    # loop by calling poll_and_handoff again would restart
                    # the whole flow; instead return no_candidates so the
                    # scheduler cycle can pick up next time).  Keep behavior
                    # conservative: don't auto-loop here to avoid unexpected
                    # extra work in the poller.
                    return PollerResult(outcome=PollerOutcome.no_candidates)
            except Exception:
                LOG.exception("Failed to parse wl show output for %s; proceeding to handler", work_id)
    except Exception:
        LOG.exception("Failed to execute wl show for %s; proceeding to handler", work_id)

    # Hand off to handler and persist timestamps based on outcome.
    # Record a per-item last_attempt_at for observability regardless of
    # success; update last_audit_at only when the handler indicates
    # success by returning a truthy value.
    try:
        success = bool(handler(selected))
    except Exception:
        LOG.exception("Audit handler raised for %s", work_id)
        success = False

    try:
        # Best-effort: record the attempt timestamp
        state.setdefault("last_attempt_at_by_item", {})
        state["last_attempt_at_by_item"][work_id] = now.isoformat()
        if success:
            state.setdefault("last_audit_at_by_item", {})
            state["last_audit_at_by_item"][work_id] = now.isoformat()
        store.update_state(spec.command_id, state)
    except Exception:
        LOG.exception(
            "Failed to persist audit timestamps for %s (success=%s)", work_id, success
        )

    return PollerResult(
        outcome=PollerOutcome.handed_off,
        selected_item_id=work_id,
    )
