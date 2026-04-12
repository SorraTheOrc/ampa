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
        selector = IntakeCandidateSelector(run_shell=self.run_shell, cwd=self.command_cwd)
        candidates = selector.query_candidates()
        if candidates is None:
            LOG.warning("Intake runner: wl list query failed")
            return {"selected": None, "error": "query_failed"}
        if not candidates:
            LOG.info("Intake runner: no idea-stage candidates")
            return {"selected": None}

        selected = selector.select_top(candidates)
        if selected is None:
            LOG.info("Intake runner: no selection made")
            return {"selected": None}

        wid = str(selected.get("id") or "")
        if not wid:
            LOG.warning("Intake runner: selected candidate missing id")
            return {"selected": None}

        LOG.info("Intake runner: selected candidate %s — %s", wid, selected.get("title") or selected.get("name") or "(no title)")

        # Persist selection timestamp to store state for observability.
        try:
            state = store.get_state(spec.command_id) or {}
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
