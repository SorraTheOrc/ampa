"""Intake runner to query and select idea-stage work items.

This module provides a small runner used by the Scheduler when a command
with command_type=="intake" (or id == "intake-selector") is executed.
It delegates to IntakeCandidateSelector to query `wl list --stage idea` and
select a single top candidate. When a candidate is chosen it records a
per-item `last_selected_at_by_item` timestamp in the scheduler store so
that subsequent cycles can observe recent selections.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .intake_selector import IntakeCandidateSelector
from . import notifications
from .engine.dispatch import IntakeDispatcher, DispatchResult
import os
import datetime as dt
import json
from datetime import timezone
import logging as _logging
from . import server as _server

LOG = logging.getLogger("ampa.intake_runner")

# Retry/backoff defaults (minutes)
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_MINUTES = 15

class IntakeRunner:
    def __init__(self, run_shell: Any, command_cwd: str):
        self.run_shell = run_shell
        self.command_cwd = command_cwd

    def run(self, spec, store) -> dict:
        """Run one intake selection cycle.

        Returns a small dict describing outcome: {"selected": id or None}.
        """
        # First, check previous dispatches for completion / input_needed / timeout
        try:
            self._process_previous_dispatches(spec, store)
        except Exception:
            LOG.exception("Failed while processing previous intake dispatch outcomes")

        selector = IntakeCandidateSelector(run_shell=self.run_shell, cwd=self.command_cwd)
        candidates = selector.query_candidates()
        if candidates is None:
            LOG.warning("Intake runner: wl list query failed")
            return {"selected": None, "error": "query_failed"}
        if not candidates:
            LOG.info("Intake runner: no idea-stage candidates")
            return {"selected": None}
        LOG.info("Intake runner (query phase): wl list returned %d candidate(s)", len(candidates))

        selected = selector.select_top(candidates)
        if selected is None:
            LOG.info("Intake runner: no selection made")
            return {"selected": None}

        wid = str(selected.get("id") or "")
        if not wid:
            LOG.warning("Intake runner: selected candidate missing id")
            return {"selected": None}

        # Before persisting selection or notifying, consult per-item retry/
        # dispatch state. If the item is currently backoff-scheduled,
        # permanently failed, or already running (live pid), skip selection.
        try:
            state = store.get_state(spec.command_id) or {}
            dispatches = state.setdefault("intake_dispatches", {})
            retries = state.setdefault("intake_retries", {})

            # Check for permanent failure recorded in retry state.
            entry = retries.get(wid) or {}
            permanent = bool(entry.get("permanent_failure", False))
            next_attempt_iso = entry.get("next_attempt")

            if permanent:
                LOG.info("Intake for %s has permanent failure recorded; skipping selection", wid)
                return {"selected": None, "skipped": "permanent_failure"}

            if next_attempt_iso:
                try:
                    next_dt = dt.datetime.fromisoformat(next_attempt_iso)
                    now = dt.datetime.now(dt.timezone.utc)
                    if now < next_dt:
                        LOG.info(
                            "Intake for %s is backoff-scheduled until %s; skipping selection",
                            wid,
                            next_attempt_iso,
                        )
                        return {"selected": None, "skipped": "backoff", "next_attempt": next_attempt_iso}
                except Exception:
                    LOG.exception("Malformed next_attempt timestamp for %s: %r", wid, next_attempt_iso)

            # If we have a recorded dispatch with a pid, prefer to avoid
            # selecting while the process is still running.
            existing = dispatches.get(wid) or {}
            existing_pid = existing.get("pid")
            if existing_pid is not None:
                # If this dispatch has already been observed as completed,
                # its pid should not block new selections. Clear stale
                # observed entries and allow selection.
                if existing.get("observed"):
                    dispatches.pop(wid, None)
                else:
                    try:
                        os.killpg(int(existing_pid), 0)
                        LOG.info("Intake for %s already in progress (pid=%s); skipping selection", wid, existing_pid)
                        return {"selected": None, "skipped": "already_running", "pid": existing_pid}
                    except ProcessLookupError:
                        # Stale record — clear it and allow selection to proceed.
                        dispatches.pop(wid, None)
                    except PermissionError:
                        LOG.info(
                            "Assuming intake %s already in progress (no permission to signal pid=%s); skipping",
                            wid,
                            existing_pid,
                        )
                        return {"selected": None, "skipped": "already_running", "pid": existing_pid}

            LOG.info("Intake runner: selected candidate %s — %s", wid, selected.get("title") or selected.get("name") or "(no title)")

            # Persist selection timestamp to store state for observability.
            state.setdefault("last_selected_at_by_item", {})
            state["last_selected_at_by_item"][wid] = dt.datetime.now(dt.timezone.utc).isoformat()
            store.update_state(spec.command_id, state)
        except Exception:
            LOG.exception("Failed to persist intake selection state for %s", wid)

        # Integration: dispatch the intake session using IntakeDispatcher,
        # notify operators, and add a worklog comment summarising the
        # dispatch result so humans can trace what happened.
        try:
            title_text = selected.get("title") or selected.get("name") or "(no title)"
            notif_title = "Automated Intake Selected"
            notif_body = f"{title_text} ({wid}) has been selected for automated intake processing."
            try:
                notifications.notify(notif_title, notif_body, message_type="intake")
            except Exception:
                LOG.exception("Failed to send intake notification for %s", wid)
        except Exception:
            LOG.exception("Failed to build/send intake notification for %s", wid)

        # Decide whether to dispatch: avoid starting a second intake process
        # for the same work item if one is already recorded as in-progress.
        LOG.info("Intake runner (dispatch phase): deciding whether to dispatch %s", wid)
        try:
            state = store.get_state(spec.command_id) or {}
            dispatches = state.setdefault("intake_dispatches", {})
            # Retry/backoff state stored alongside dispatch tracking.
            retries = state.setdefault("intake_retries", {})
        except Exception:
            LOG.exception("Failed to read scheduler state for intake dispatch tracking")
            dispatches = {}
            retries = {}

        already_running = False
        existing_pid = None
        if wid in dispatches:
            existing = dispatches.get(wid) or {}
            existing_pid = existing.get("pid")
            if existing_pid is not None:
                # If the dispatch was already observed, don't treat its pid as
                # blocking — clear and proceed. Otherwise check process group.
                if existing.get("observed"):
                    already_running = False
                    dispatches.pop(wid, None)
                else:
                    try:
                        # If the process group exists this will succeed; otherwise
                        # ProcessLookupError is raised.
                        os.killpg(int(existing_pid), 0)
                        already_running = True
                    except ProcessLookupError:
                        # Stale record — clear it and allow new dispatch.
                        already_running = False
                        dispatches.pop(wid, None)
                    except PermissionError:
                        # We can't signal the group, assume it's running to be safe.
                        already_running = True

        dispatch_result: DispatchResult | None = None
        if already_running:
            LOG.info("Intake for %s already in progress (pid=%s); skipping new dispatch", wid, existing_pid)
            # Create a synthetic successful DispatchResult referencing the
            # existing pid so downstream code and comments can include it.
            try:
                dispatch_result = DispatchResult(
                    success=True,
                    command=f"/intake {wid}",
                    work_item_id=wid,
                    timestamp=dt.datetime.now(dt.timezone.utc),
                    pid=int(existing_pid),
                )
            except Exception:
                dispatch_result = None
        else:
            # Metadata-driven overrides for retry/backoff behaviour.
            meta = getattr(spec, "metadata", {}) or {}
            try:
                max_retries = int(meta.get("max_retries", DEFAULT_MAX_RETRIES))
            except (TypeError, ValueError):
                max_retries = DEFAULT_MAX_RETRIES
            try:
                backoff_base_minutes = float(
                    meta.get("backoff_base_minutes", DEFAULT_BACKOFF_BASE_MINUTES)
                )
            except (TypeError, ValueError):
                backoff_base_minutes = DEFAULT_BACKOFF_BASE_MINUTES

            # Per-item retry entry
            entry = retries.get(wid) or {}
            attempts = int(entry.get("attempts", 0))
            next_attempt_iso = entry.get("next_attempt")
            permanent = bool(entry.get("permanent_failure", False))

            if permanent:
                LOG.info("Intake for %s has permanent failure recorded; skipping dispatch", wid)
                dispatch_result = DispatchResult(
                    success=False,
                    command=f"/intake {wid}",
                    work_item_id=wid,
                    timestamp=dt.datetime.now(dt.timezone.utc),
                    pid=None,
                    error="permanent_failure",
                )
            else:
                # If a future next_attempt is set, skip until due.
                if next_attempt_iso:
                    try:
                        next_dt = dt.datetime.fromisoformat(next_attempt_iso)
                        now = dt.datetime.now(dt.timezone.utc)
                        if now < next_dt:
                            LOG.info(
                                "Intake for %s is backoff-scheduled until %s; skipping",
                                wid,
                                next_attempt_iso,
                            )
                            dispatch_result = None
                    except Exception:
                        LOG.exception("Malformed next_attempt timestamp for %s: %r", wid, next_attempt_iso)

                if dispatch_result is None:
                    try:
                        dispatcher = IntakeDispatcher()
                        dispatch_result = dispatcher.dispatch(command="", work_item_id=wid)
                    except Exception:
                        LOG.exception("Intake dispatch failed for %s", wid)
                        dispatch_result = None

            # Persist the dispatch record for future deduplication.
            try:
                if dispatch_result is not None:
                    dispatches[wid] = {
                        "pid": getattr(dispatch_result, "pid", None),
                        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "success": bool(getattr(dispatch_result, "success", False)),
                    }

                # Update retry/backoff tracking based on dispatch outcome.
                try:
                    # Re-read entry in case other cycles modified it.
                    entry = retries.get(wid) or {}
                    attempts = int(entry.get("attempts", 0))
                    permanent = bool(entry.get("permanent_failure", False))

                    if dispatch_result is None:
                        # Internal error during dispatch attempt — increment attempts
                        attempts += 1
                        if attempts >= max_retries:
                            # Mark permanent failure and notify operators.
                            retries[wid] = {
                                "attempts": attempts,
                                "permanent_failure": True,
                                "next_attempt": None,
                            }
                            permanent = True
                            try:
                                notifications.notify(
                                    f"Intake dispatch permanent failure — {wid}",
                                    f"Automated intake dispatch for {wid} failed after {attempts} attempt(s).",
                                    message_type="error",
                                )
                            except Exception:
                                LOG.exception("Failed to send permanent failure notification for %s", wid)
                        else:
                            # schedule next attempt with exponential backoff
                            delay_minutes = backoff_base_minutes * (2 ** (attempts - 1))
                            next_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delay_minutes)
                            retries[wid] = {
                                "attempts": attempts,
                                "permanent_failure": False,
                                "next_attempt": next_dt.isoformat(),
                            }
                    else:
                        if dispatch_result.success:
                            # Clear retry state on success
                            if wid in retries:
                                retries.pop(wid, None)
                        else:
                            # Dispatch attempted but returned failure result
                            attempts = attempts + 1
                            if attempts >= max_retries:
                                retries[wid] = {
                                    "attempts": attempts,
                                    "permanent_failure": True,
                                    "next_attempt": None,
                                }
                                try:
                                    notifications.notify(
                                        f"Intake dispatch permanent failure — {wid}",
                                        f"Automated intake dispatch for {wid} failed after {attempts} attempt(s). Error: {dispatch_result.error}",
                                        message_type="error",
                                    )
                                except Exception:
                                    LOG.exception("Failed to send permanent failure notification for %s", wid)
                            else:
                                delay_minutes = backoff_base_minutes * (2 ** (attempts - 1))
                                next_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delay_minutes)
                                retries[wid] = {
                                    "attempts": attempts,
                                    "permanent_failure": False,
                                    "next_attempt": next_dt.isoformat(),
                                }
                except Exception:
                    LOG.exception("Failed to update intake_retries state for %s", wid)

                store.update_state(spec.command_id, state)
            except Exception:
                LOG.exception("Failed to persist intake dispatch state for %s", wid)
        LOG.info("Intake runner (dispatch phase): dispatch result for %s = %s", wid, getattr(dispatch_result, 'success', None))

        # Add a Worklog comment summarising the dispatch result so operators
        # can see whether the intake session was started and any error.
        try:
            if dispatch_result is None:
                comment_text = "Automated intake selected by AMPA. Dispatch attempt failed: internal error."
            else:
                if dispatch_result.success:
                    comment_text = (
                        f"Automated intake dispatched by AMPA. pid={dispatch_result.pid}."
                        if getattr(dispatch_result, "pid", None)
                        else "Automated intake dispatch recorded."
                    )
                else:
                    comment_text = (
                        f"Automated intake dispatch failed: {dispatch_result.error or 'unknown error'}."
                    )

            cmd = f"wl comment add {wid} --comment \"{comment_text}\" --author \"ampa\" --json"
            try:
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd, timeout=60)
            except TypeError:
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd)
        except Exception:
            LOG.exception("Failed to add Worklog comment for selected intake candidate %s", wid)
        return {"selected": wid, "dispatch": getattr(dispatch_result, "success", False)}

    def _process_previous_dispatches(self, spec, store) -> None:
        """Inspect previously-recorded intake dispatches and record outcomes.

        For each recorded dispatch in the store state under ``intake_dispatches``
        that has not already been observed, fetch the work item via ``wl show``
        and detect whether the intake run completed (stage=="intake_complete"),
        required more input (status=="input_needed"), or timed out (older than
        AMPA_INTAKE_COMPLETION_TIMEOUT seconds).  Outcomes are recorded in the
        per-command state under ``intake_metrics`` and the dispatch entry is
        annotated so it is not processed repeatedly.
        """
        # timeout default: 4 hours (can be overridden by env var)
        try:
            timeout_seconds = int(os.environ.get("AMPA_INTAKE_COMPLETION_TIMEOUT", 4 * 3600))
        except Exception:
            timeout_seconds = 4 * 3600

        state = store.get_state(spec.command_id) or {}
        dispatches = state.setdefault("intake_dispatches", {})
        metrics = state.setdefault("intake_metrics", {})

        now = dt.datetime.now(timezone.utc)

        for wid, entry in list(dispatches.items()):
            try:
                if entry.get("observed"):
                    continue

                started_iso = entry.get("started_at")
                if not started_iso:
                    # No timestamp; mark observed to avoid repeated work.
                    entry["observed"] = True
                    continue

                try:
                    started_dt = dt.datetime.fromisoformat(started_iso)
                except Exception:
                    # Malformed timestamp: mark observed and continue.
                    entry["observed"] = True
                    continue

                elapsed = (now - started_dt).total_seconds()

                outcome = None
                # If past configured timeout, treat as timeout without querying WL
                if elapsed >= timeout_seconds:
                    outcome = "timeout"
                    completed_at = now.isoformat()
                else:
                    # Query work item to detect stage/status
                    cmd = f"wl show {wid} --children --json"
                    try:
                        proc = self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd, timeout=30)
                    except TypeError:
                        # Some test fakes do not accept timeout kwarg
                        proc = self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd)
                    except Exception:
                        proc = None

                    if proc is None or getattr(proc, "returncode", 0) != 0:
                        # Unable to observe yet; skip processing this entry for now.
                        continue

                    try:
                        payload = json.loads(proc.stdout or "null")
                    except Exception:
                        # Malformed output; skip for now
                        continue

                    # Normalise wl show shapes
                    wi = None
                    if isinstance(payload, dict):
                        if isinstance(payload.get("workItem"), dict):
                            wi = payload.get("workItem")
                        elif isinstance(payload.get("workItems"), list) and payload.get("workItems"):
                            wi = payload.get("workItems")[0]
                        else:
                            wi = payload

                    if not isinstance(wi, dict):
                        continue

                    stage = wi.get("stage")
                    status = wi.get("status")

                    if stage == "intake_complete":
                        outcome = "intake_complete"
                        completed_at = now.isoformat()
                    elif status == "input_needed":
                        outcome = "input_needed"
                        completed_at = now.isoformat()

                if outcome:
                    # Record metric
                    dur = None
                    try:
                        dur = int(elapsed) if elapsed is not None else None
                    except Exception:
                        dur = None

                    metrics[wid] = {
                        "started_at": started_iso,
                        "completed_at": completed_at,
                        "outcome": outcome,
                        "duration_seconds": dur,
                    }

                    # Update aggregated per-item stats (processed, successes, failures, avg duration)
                    stats = state.setdefault("intake_stats", {})
                    st = stats.setdefault(wid, {"processed": 0, "successes": 0, "failures": 0, "total_duration_seconds": 0, "avg_duration_seconds": None})
                    try:
                        st["processed"] = int(st.get("processed", 0)) + 1
                        if outcome == "intake_complete":
                            st["successes"] = int(st.get("successes", 0)) + 1
                        else:
                            st["failures"] = int(st.get("failures", 0)) + 1
                        if dur is not None:
                            st["total_duration_seconds"] = int(st.get("total_duration_seconds", 0)) + int(dur)
                            st["avg_duration_seconds"] = int(st["total_duration_seconds"] / st["processed"]) if st["processed"] > 0 else None
                    except Exception:
                        LOG.exception("Failed to update intake_stats for %s", wid)

                    # Annotate dispatch entry so we don't re-process it.
                    entry["observed"] = True
                    entry.setdefault("outcome", outcome)
                    entry.setdefault("completed_at", completed_at)
                    # Clear the recorded pid for completed/observed dispatches so
                    # later selection cycles do not mistake a stale pid for an
                    # in-progress intake run (which would cause skipping).
                    entry.pop("pid", None)

                    # Log detect/assign phase details
                    try:
                        assignee = None
                        if isinstance(wi, dict):
                            assignee = wi.get("assignee")
                        LOG.info("Intake runner (detect phase): outcome=%s for %s (assignee=%s, duration=%s)", outcome, wid, assignee, dur)
                    except Exception:
                        LOG.exception("Failed to log detect/assign info for %s", wid)

                    # Persist state after each processed entry for durability.
                    try:
                        state["intake_metrics"] = metrics
                        state["intake_dispatches"] = dispatches
                        state["intake_stats"] = stats
                        store.update_state(spec.command_id, state)
                        # Update Prometheus metrics exposed by server module
                        try:
                            # Aggregate across per-item stats
                            total_processed = 0
                            total_successes = 0
                            total_duration = 0
                            for s in stats.values():
                                try:
                                    total_processed += int(s.get("processed", 0))
                                except Exception:
                                    pass
                                try:
                                    total_successes += int(s.get("successes", 0))
                                except Exception:
                                    pass
                                try:
                                    total_duration += int(s.get("total_duration_seconds", 0))
                                except Exception:
                                    pass

                            if total_processed > 0:
                                success_rate = float(total_successes) / float(total_processed)
                                avg_completion = float(total_duration) / float(total_processed) if total_duration is not None else 0.0
                            else:
                                success_rate = 0.0
                                avg_completion = 0.0

                            # Increment the counter by the delta since last update (server tracks last value)
                            try:
                                delta = total_processed - getattr(_server, "_last_intake_processed_total", 0)
                                if delta > 0:
                                    _server.ampa_intake_items_processed_total.inc(delta)
                                # store last value for next delta computation
                                _server._last_intake_processed_total = total_processed
                            except Exception:
                                # Best-effort: if counter manipulation fails, ignore
                                pass

                            try:
                                _server.ampa_intake_success_rate.set(success_rate)
                                _server.ampa_intake_avg_completion_seconds.set(avg_completion)
                            except Exception:
                                LOG.exception("Failed to set Prometheus intake gauges")
                        except Exception:
                            LOG.exception("Failed to update Prometheus intake metrics")
                    except Exception:
                        LOG.exception("Failed to persist intake dispatch outcome for %s", wid)
            except Exception:
                LOG.exception("Unexpected error while inspecting dispatch %s", wid)
