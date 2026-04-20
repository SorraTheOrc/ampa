"""Plan runner to query intake_complete work items and dispatch `/plan`.

This mirrors the intake_runner structure but is focused on progressing
work items from intake_complete -> plan_complete by running an opencode
`/plan {id}` session via the dispatch system.  It keeps separate scheduler
state namespaces (`plan_dispatches`, `plan_retries`, `plan_metrics`) so
intake and plan runs are tracked independently.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from datetime import timezone
from typing import Any

from .intake_selector import IntakeCandidateSelector  # reuse helper shape
from .engine.dispatch import OpenCodeRunDispatcher, DispatchResult
from . import notifications

LOG = logging.getLogger("ampa.plan_runner")

# Retry/backoff defaults (minutes)
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_MINUTES = 15


class PlanDispatcher:
    """Dispatch wrapper specialised for spawning `/plan` runs.

    Builds a canonical plan command and delegates to an underlying
    dispatcher (OpenCodeRunDispatcher by default).
    """

    def __init__(self, runner: Any | None = None, timeout: int | None = None, clock: Any = None):
        self._runner = runner or OpenCodeRunDispatcher()
        self._clock = clock or (lambda: dt.datetime.now(timezone.utc))
        env_val = os.environ.get("AMPA_PLAN_TIMEOUT")
        if env_val is not None:
            try:
                self._timeout = int(env_val)
            except Exception:
                self._timeout = timeout or 3600
        else:
            self._timeout = timeout or 3600

    def dispatch(self, command: str, work_item_id: str) -> DispatchResult:
        ts = self._clock()
        plan_cmd = f"opencode run --agent Casey --command plan {work_item_id}"
        LOG.info("PlanDispatcher dispatching %s: %s", work_item_id, plan_cmd)
        result = self._runner.dispatch(plan_cmd, work_item_id)
        # Preserve timestamp if underlying runner set it
        if result.timestamp is None:
            result = DispatchResult(
                success=result.success,
                command=result.command,
                work_item_id=result.work_item_id,
                timestamp=ts,
                pid=result.pid,
                error=result.error,
                container_id=getattr(result, "container_id", None),
            )
        return result


class PlanCandidateSelector(IntakeCandidateSelector):
    """Selector for plan candidates. Reuses IntakeCandidateSelector but queries
    for `intake_complete` stage.
    """

    def query_candidates(self, timeout: int = 60):
        try:
            proc = self.run_shell(
                "wl next --stage intake_complete --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=timeout,
            )
        except Exception:
            LOG.exception("wl next --stage intake_complete command failed to execute")
            return None

        if proc.returncode != 0:
            LOG.warning("wl next --stage intake_complete exited with code %s: %s", proc.returncode, proc.stderr)
            return None

        try:
            raw = json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl next --stage intake_complete output as JSON")
            return None

        items = []
        if isinstance(raw, list):
            items.extend([it for it in raw if isinstance(it, dict)])
        elif isinstance(raw, dict):
            for key in ("workItems", "work_items", "items", "data"):
                val = raw.get(key)
                if isinstance(val, list):
                    items.extend([it for it in val if isinstance(it, dict)])
                    break
            if not items:
                for k, v in raw.items():
                    if isinstance(v, list) and k.lower().endswith("workitems"):
                        items.extend([it for it in v if isinstance(it, dict)])
                        break

        normalized = []
        for it in items:
            wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
            if not wid:
                continue
            candidate = dict(it)
            candidate["id"] = str(wid)
            normalized.append(candidate)

        return normalized


class PlanRunner:
    def __init__(self, run_shell: Any, command_cwd: str):
        self.run_shell = run_shell
        self.command_cwd = command_cwd

    def run(self, spec, store) -> dict:
        """Run one plan-selection cycle.

        Returns a small dict describing outcome: {"planned": id or None}.
        """
        # First, check previous dispatches for completion / timeout
        try:
            self._process_previous_dispatches(spec, store)
        except Exception:
            LOG.exception("Failed while processing previous plan dispatch outcomes")

        selector = PlanCandidateSelector(run_shell=self.run_shell, cwd=self.command_cwd)
        candidates = selector.query_candidates()
        if candidates is None:
            LOG.warning("Plan runner: wl next query failed")
            return {"planned": None, "error": "query_failed"}
        if not candidates:
            LOG.info("Plan runner: no intake_complete candidates")
            return {"planned": None}

        selected = selector.select_top(candidates)
        if selected is None:
            LOG.info("Plan runner: no selection made")
            return {"planned": None}

        wid = str(selected.get("id") or "")
        if not wid:
            LOG.warning("Plan runner: selected candidate missing id")
            return {"planned": None}

        LOG.info("Plan runner: selected candidate %s — %s", wid, selected.get("title") or selected.get("name") or "(no title)")

        # Integration: notify operators of plan candidate selection
        try:
            title_text = selected.get("title") or selected.get("name") or "(no title)"
            notif_title = "Automated Plan Selected"
            notif_body = f"{title_text} ({wid}) has been selected for automated planning."
            try:
                notifications.notify(notif_title, notif_body, message_type="plan")
            except Exception:
                LOG.exception("Failed to send plan notification for %s", wid)
        except Exception:
            LOG.exception("Failed to build/send plan notification for %s", wid)

        # Read state
        try:
            state = store.get_state(spec.command_id) or {}
            dispatches = state.setdefault("plan_dispatches", {})
            retries = state.setdefault("plan_retries", {})
        except Exception:
            LOG.exception("Failed to read scheduler state for plan dispatch tracking")
            dispatches = {}
            retries = {}

        # Check existing recorded dispatch
        already_running = False
        existing_pid = None
        if wid in dispatches:
            existing = dispatches.get(wid) or {}
            existing_pid = existing.get("pid")
            if existing_pid is not None:
                if existing.get("observed"):
                    # clear stale observed
                    dispatches.pop(wid, None)
                else:
                    try:
                        os.killpg(int(existing_pid), 0)
                        already_running = True
                    except ProcessLookupError:
                        already_running = False
                        dispatches.pop(wid, None)
                    except PermissionError:
                        already_running = True

        dispatch_result = None
        if already_running:
            LOG.info("Plan for %s already in progress (pid=%s); skipping new dispatch", wid, existing_pid)
            try:
                dispatch_result = DispatchResult(
                    success=True,
                    command=f"/plan {wid}",
                    work_item_id=wid,
                    timestamp=dt.datetime.now(timezone.utc),
                    pid=int(existing_pid),
                )
            except Exception:
                dispatch_result = None
        else:
            meta = getattr(spec, "metadata", {}) or {}
            try:
                max_retries = int(meta.get("max_retries", DEFAULT_MAX_RETRIES))
            except Exception:
                max_retries = DEFAULT_MAX_RETRIES
            try:
                backoff_base_minutes = float(meta.get("backoff_base_minutes", DEFAULT_BACKOFF_BASE_MINUTES))
            except Exception:
                backoff_base_minutes = DEFAULT_BACKOFF_BASE_MINUTES

            entry = retries.get(wid) or {}
            attempts = int(entry.get("attempts", 0))
            next_attempt_iso = entry.get("next_attempt")
            permanent = bool(entry.get("permanent_failure", False))

            if permanent:
                LOG.info("Plan for %s has permanent failure recorded; skipping dispatch", wid)
                dispatch_result = DispatchResult(
                    success=False,
                    command=f"/plan {wid}",
                    work_item_id=wid,
                    timestamp=dt.datetime.now(timezone.utc),
                    pid=None,
                    error="permanent_failure",
                )
            else:
                if next_attempt_iso:
                    try:
                        next_dt = dt.datetime.fromisoformat(next_attempt_iso)
                        now = dt.datetime.now(dt.timezone.utc)
                        if now < next_dt:
                            LOG.info("Plan for %s is backoff-scheduled until %s; skipping", wid, next_attempt_iso)
                            dispatch_result = None
                    except Exception:
                        LOG.exception("Malformed next_attempt timestamp for %s: %r", wid, next_attempt_iso)

                if dispatch_result is None:
                    try:
                        dispatcher = PlanDispatcher()
                        dispatch_result = dispatcher.dispatch(command="", work_item_id=wid)
                    except Exception:
                        LOG.exception("Plan dispatch failed for %s", wid)
                        dispatch_result = None

            # Persist the dispatch record and update retries
            try:
                if dispatch_result is not None:
                    dispatches[wid] = {
                        "pid": getattr(dispatch_result, "pid", None),
                        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "success": bool(getattr(dispatch_result, "success", False)),
                    }

                try:
                    entry = retries.get(wid) or {}
                    attempts = int(entry.get("attempts", 0))
                    permanent = bool(entry.get("permanent_failure", False))

                    if dispatch_result is None:
                        attempts += 1
                        if attempts >= max_retries:
                            retries[wid] = {"attempts": attempts, "permanent_failure": True, "next_attempt": None}
                            permanent = True
                            try:
                                notifications.notify(
                                    f"Plan dispatch permanent failure — {wid}",
                                    f"Automated plan dispatch for {wid} failed after {attempts} attempt(s).",
                                    message_type="error",
                                )
                            except Exception:
                                LOG.exception("Failed to send permanent failure notification for %s", wid)
                        else:
                            delay_minutes = backoff_base_minutes * (2 ** (attempts - 1))
                            next_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delay_minutes)
                            retries[wid] = {"attempts": attempts, "permanent_failure": False, "next_attempt": next_dt.isoformat()}
                    else:
                        if dispatch_result.success:
                            if wid in retries:
                                retries.pop(wid, None)
                        else:
                            attempts = attempts + 1
                            if attempts >= max_retries:
                                retries[wid] = {"attempts": attempts, "permanent_failure": True, "next_attempt": None}
                                try:
                                    notifications.notify(
                                        f"Plan dispatch permanent failure — {wid}",
                                        f"Automated plan dispatch for {wid} failed after {attempts} attempt(s). Error: {dispatch_result.error}",
                                        message_type="error",
                                    )
                                except Exception:
                                    LOG.exception("Failed to send permanent failure notification for %s", wid)
                            else:
                                delay_minutes = backoff_base_minutes * (2 ** (attempts - 1))
                                next_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delay_minutes)
                                retries[wid] = {"attempts": attempts, "permanent_failure": False, "next_attempt": next_dt.isoformat()}
                except Exception:
                    LOG.exception("Failed to update plan_retries state for %s", wid)

                state["plan_dispatches"] = dispatches
                state["plan_retries"] = retries
                store.update_state(spec.command_id, state)
            except Exception:
                LOG.exception("Failed to persist plan dispatch state for %s", wid)

        # Add a Worklog comment summarising the dispatch result
        try:
            if dispatch_result is None:
                comment_text = "Automated plan-runner selected by AMPA. Dispatch attempt failed: internal error."
            else:
                if dispatch_result.success:
                    comment_text = (
                        f"Automated plan dispatched by AMPA. pid={dispatch_result.pid}."
                        if getattr(dispatch_result, "pid", None)
                        else "Automated plan dispatch recorded."
                    )
                else:
                    comment_text = f"Automated plan dispatch failed: {dispatch_result.error or 'unknown error'}."

            cmd = f"wl comment add {wid} --comment \"{comment_text}\" --author \"ampa\" --json"
            try:
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd, timeout=60)
            except TypeError:
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd)
        except Exception:
            LOG.exception("Failed to add Worklog comment for plan candidate %s", wid)

        return {"planned": wid, "dispatch": getattr(dispatch_result, "success", False)}

    def _process_previous_dispatches(self, spec, store) -> None:
        """Inspect previously-recorded plan dispatches and record outcomes.

        For each recorded dispatch in `plan_dispatches` not observed, fetch
        the work item and detect whether the plan run completed (stage=="plan_complete")
        or timed out (older than AMPA_PLAN_COMPLETION_TIMEOUT seconds). Outcomes
        are recorded in `plan_metrics` and the dispatch entry is annotated with
        `observed` to avoid repeated processing.
        """
        try:
            timeout_seconds = int(os.environ.get("AMPA_PLAN_COMPLETION_TIMEOUT", 4 * 3600))
        except Exception:
            timeout_seconds = 4 * 3600

        state = store.get_state(spec.command_id) or {}
        dispatches = state.setdefault("plan_dispatches", {})
        metrics = state.setdefault("plan_metrics", {})

        now = dt.datetime.now(timezone.utc)

        for wid, entry in list(dispatches.items()):
            try:
                if entry.get("observed"):
                    continue

                started_iso = entry.get("started_at")
                if not started_iso:
                    entry["observed"] = True
                    continue

                try:
                    started_dt = dt.datetime.fromisoformat(started_iso)
                except Exception:
                    entry["observed"] = True
                    continue

                elapsed = (now - started_dt).total_seconds()

                outcome = None
                if elapsed >= timeout_seconds:
                    outcome = "timeout"
                    completed_at = now.isoformat()
                else:
                    cmd = f"wl show {wid} --children --json"
                    try:
                        proc = self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd, timeout=30)
                    except TypeError:
                        proc = self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd)
                    except Exception:
                        proc = None

                    if proc is None or getattr(proc, "returncode", 0) != 0:
                        continue

                    try:
                        payload = json.loads(proc.stdout or "null")
                    except Exception:
                        continue

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
                    if stage == "plan_complete":
                        outcome = "plan_complete"
                        completed_at = now.isoformat()

                if outcome:
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
                    entry["observed"] = True
                    entry.setdefault("outcome", outcome)
                    entry.setdefault("completed_at", completed_at)
                    entry.pop("pid", None)

                    try:
                        state["plan_metrics"] = metrics
                        state["plan_dispatches"] = dispatches
                        store.update_state(spec.command_id, state)
                    except Exception:
                        LOG.exception("Failed to persist plan dispatch outcome for %s", wid)
            except Exception:
                LOG.exception("Unexpected error while inspecting plan dispatch %s", wid)
