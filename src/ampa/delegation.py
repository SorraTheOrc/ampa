"""Delegation orchestration extracted from ampa.scheduler.

Provides ``DelegationOrchestrator`` which encapsulates the delegation-specific
flows: pre/post reports, idle delegation execution, stale delegation recovery,
report building and Discord notification interactions.

Module-level helpers (``_content_hash``, ``_summarize_for_discord``,
``_build_delegation_report``, etc.) are also defined here and re-exported by
``ampa.scheduler`` for backward compatibility.

This module intentionally keeps no external side-effects at import time so it
is safe to import from tests and other modules.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

from .engine.candidates import CandidateSelector
from .engine.core import Engine, EngineResult, EngineStatus
from .scheduler_types import (
    _utc_now,
    _from_iso,
    _bool_meta,
    CommandRunResult,
    RunResult,
)

LOG = logging.getLogger("ampa.delegation")


# ---------------------------------------------------------------------------
# Utility / formatting helpers
# ---------------------------------------------------------------------------


def _trim_text(value: Optional[str]) -> str:
    return value.strip() if value else ""


def _normalize_in_progress_output(text: str) -> str:
    """Normalize in_progress output to make it idempotent for deduplication.
    
    Filters out lines or parts of lines that change between runs but don't
    affect the actual work items being reported (e.g., timestamps, summary
    counts that may vary in formatting).
    
    This ensures that identical work item lists produce identical report
    hashes and suppress duplicate Discord notifications.
    """
    if not text:
        return text
    
    lines = text.splitlines()
    normalized_lines = []
    
    for line in lines:
        stripped = line.strip()
        
        # Skip lines that are just summary counts or timestamps
        # These change between runs even when work items are identical
        if not stripped:
            continue
        
        # Remove common timestamp patterns at the start or end of lines
        # e.g., "then two minutes later", timestamps like "2026-03-21 10:30:00"
        normalized = stripped
        
        # Remove "then X minutes/hours later" patterns (standalone lines or partial)
        # Match both numeric ("then 2 minutes later") and word forms ("then two minutes later")
        normalized = re.sub(r'^then\s+\w+\s+(minutes?|hours?|seconds?)\s+later\s*$', '', normalized, flags=re.IGNORECASE)
        
        # Remove timestamps (ISO format or similar)
        normalized = re.sub(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', normalized)
        normalized = re.sub(r'\b\d{2}:\d{2}:\d{2}\b', '', normalized)
        
        # Remove common summary prefixes that may vary
        # Handle "Found X in-progress work item(s)" and "Total: X items in-progress"
        normalized = re.sub(r'^(Found\s+\d+\s+in-progress\s+work\s+item\(s\)|Total:\s+\d+\s+items\s+in-progress)\s*$', '', normalized, flags=re.IGNORECASE)
        # Also handle variations like "In Progress", "In-progress", "Eight work items are in-progress"
        normalized = re.sub(r'^(In Progress|In-progress|Eight|Found|Total)\s*[:\-]?\s*', '', normalized, flags=re.IGNORECASE)
        
        # Skip if line becomes empty after normalization
        if not normalized.strip():
            continue
        
        normalized_lines.append(normalized)
    
    # Rebuild the text, preserving the core work item lines
    result = '\n'.join(normalized_lines)
    
    # If we filtered everything, return original to preserve at least some content
    return result if result.strip() else text.strip()


def _content_hash(text: Optional[str]) -> str:
    """Return a SHA-256 hex digest of *text* for change detection.

    Used to suppress duplicate delegation report Discord messages when the
    report content has not changed since the previous run.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _summarize_for_discord(text: Optional[str], max_chars: int = 2000) -> str:
    """If text is longer than max_chars, call ``opencode run`` to produce a short summary.

    Returns the original text on any failure.
    """
    if not text:
        return ""
    try:
        if len(text) <= max_chars:
            return text
        # avoid passing extremely large blobs to the CLI; cap input size
        cap = 20000
        input_text = text[:cap]
        cmd = [
            "opencode",
            "run",
            f"summarize this content in under {max_chars} characters: {input_text}",
        ]
        LOG.info("Summarizing content for Discord (len=%d) via opencode", len(text))
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            LOG.warning(
                "opencode summarizer failed rc=%s stderr=%r",
                getattr(proc, "returncode", None),
                getattr(proc, "stderr", None),
            )
            return text
        summary = (proc.stdout or "").strip()
        if not summary:
            return text
        return summary
    except Exception:
        LOG.exception("Failed to summarize content for Discord")
        return text


def _format_in_progress_items(text: str) -> List[str]:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    items: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if "- SA-" not in stripped:
            continue
        cleaned = stripped.lstrip("├└│ ")
        items.append(cleaned)
    return items


def _format_candidate_line(candidate: Dict[str, Any]) -> str:
    work_id = str(candidate.get("id") or "?")
    title = candidate.get("title") or candidate.get("name") or "(no title)"
    status = candidate.get("status") or candidate.get("stage") or ""
    priority = candidate.get("priority")
    parts = [f"{title} - {work_id}"]
    meta: List[str] = []
    if status:
        meta.append(f"status: {status}")
    if priority is not None:
        meta.append(f"priority: {priority}")
    if meta:
        parts.append("(" + ", ".join(meta) + ")")
    return " ".join(parts)


def _build_dry_run_report(
    *,
    in_progress_output: str,
    candidates: List[Dict[str, Any]],
    top_candidate: Optional[Dict[str, Any]],
    skip_reasons: Optional[List[str]] = None,
    rejections: Optional[List[Dict[str, str]]] = None,
) -> str:
    # If there are in-progress items, produce a concise, operator-friendly
    # message listing those items and skip the verbose candidate/top-candidate
    # sections. This keeps the operator-facing output short when agents are
    # actively working.
    in_progress_items = _format_in_progress_items(in_progress_output)
    if in_progress_items:
        lines: List[str] = ["Agents are currently busy with:"]
        for item in in_progress_items:
    # match the visual style requested (em dash bullets)
            lines.append(f"── {item}")
        return "\n".join(lines)

    # no in-progress items -> produce full report
    sections: List[str] = []
    sections.append("AMPA Delegation")
    sections.append("In-progress items:")
    sections.append("- (none)")

    sections.append("Candidates:")
    if candidates:
        for cand in candidates:
            sections.append(f"- {_format_candidate_line(cand)}")
    else:
        sections.append("- (none)")

    sections.append("Top candidate:")
    if top_candidate:
        sections.append(f"- {_format_candidate_line(top_candidate)}")
        sections.append("Rationale: selected by wl next (highest priority ready item).")
    else:
        sections.append("- (none)")
        sections.append("Rationale: no candidates returned by wl next.")

    # Surface rejection reasons when candidates were evaluated but rejected
    if rejections:
        sections.append("Rejected candidates:")
        for rej in rejections:
            rej_id = rej.get("id", "?")
            rej_title = rej.get("title", "(unknown)")
            rej_reason = rej.get("reason", "rejected")
            sections.append(f"- {rej_title} - {rej_id}: {rej_reason}")

    # Surface skip reasons (invariant failures, no candidates, etc.)
    if skip_reasons:
        sections.append("Delegation skip reasons:")
        for reason in skip_reasons:
            sections.append(f"- {reason}")

    if not candidates and not top_candidate:
        sections.append(
            "Summary: delegation is idle (no in-progress items or candidates)."
        )

    return "\n".join(sections)


def _build_dry_run_discord_message(report: str) -> str:
    summary = _summarize_for_discord(report, max_chars=1000)
    if summary and summary.strip():
        return summary
    LOG.warning("Dry-run discord summary was empty; falling back to raw report content")
    if report and report.strip():
        return report.strip()
    return "(no report details)"


def _build_delegation_report(
    *,
    in_progress_output: str,
    candidates: List[Dict[str, Any]],
    top_candidate: Optional[Dict[str, Any]],
    skip_reasons: Optional[List[str]] = None,
    rejections: Optional[List[Dict[str, str]]] = None,
) -> str:
    return _build_dry_run_report(
        in_progress_output=in_progress_output,
        candidates=candidates,
        top_candidate=top_candidate,
        skip_reasons=skip_reasons,
        rejections=rejections,
    )


def _build_delegation_discord_message(report: str) -> str:
    return _build_dry_run_discord_message(report)


# ---------------------------------------------------------------------------
# DelegationOrchestrator
# ---------------------------------------------------------------------------


class DelegationOrchestrator:
    """Encapsulates delegation orchestration formerly in ``Scheduler``.

    Accepts the same infrastructure dependencies that the scheduler used
    (``store``, ``run_shell``, ``command_cwd``, ``engine``,
    ``candidate_selector``) and exposes a small public API:

    * ``execute(spec, run)`` — full delegation flow called from
      ``Scheduler.start_command()``
    * ``run_idle_delegation(audit_only, spec)`` — engine wrapper used by
      audit and the delegation flow
    * ``run_delegation_report(spec)`` — human-readable report generation
    * ``recover_stale_delegations()`` — stale delegation watchdog
    """

    def __init__(
        self,
        store: Any,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        engine: Optional[Engine] = None,
        candidate_selector: Optional[CandidateSelector] = None,
        # Lazy imports to avoid circular dependency at module load time.
        # Callers pass these modules/functions so this module does not import
        # ``ampa.notifications`` or ``ampa.selection`` directly.
        notifications_module: Any = None,
        selection_module: Any = None,
    ) -> None:
        self.store = store
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self.engine = engine
        self._candidate_selector = candidate_selector
        self._notifications_module = notifications_module
        self._selection_module = selection_module

    # -- report dedup -------------------------------------------------------

    def _is_delegation_report_changed(self, command_id: str, report_text: str) -> bool:
        """Check whether the delegation report content has changed.

        Compares a SHA-256 hash of *report_text* against the hash stored in the
        scheduler state under ``last_delegation_report_hash``.  Returns True if
        the content differs (or no previous hash exists) and updates the stored
        hash.  Returns False when the content is identical to suppress duplicate
        Discord messages.
        """
        new_hash = _content_hash(report_text)
        state = self.store.get_state(command_id)
        old_hash = state.get("last_delegation_report_hash")
        if old_hash == new_hash:
            LOG.info(
                "Delegation report unchanged (hash=%s); suppressing Discord notification",
                new_hash[:12],
            )
            return False
        # Content changed – persist the new hash
        state["last_delegation_report_hash"] = new_hash
        self.store.update_state(command_id, state)
        LOG.info(
            "Delegation report changed (old=%s new=%s); sending Discord notification",
            (old_hash or "(none)")[:12],
            new_hash[:12],
        )
        return True

    # -- inspect idle delegation --------------------------------------------

    def _inspect_idle_delegation(self) -> Dict[str, Any]:
        """Lightweight pre-flight check for delegation state.

        Uses the engine's ``CandidateSelector`` to determine whether agents
        are idle and whether there is a candidate to delegate.  Returns a
        status dict consumed by ``execute()`` to decide what to print
        and whether to proceed with full engine delegation.

        Possible status values:
        - ``"in_progress"`` — work is already in progress (items in dict)
        - ``"idle_no_candidate"`` — idle but no actionable candidates
        - ``"idle_with_candidate"`` — idle with a selected candidate (raw dict)
        - ``"error"`` — the pre-flight check itself failed
        """
        selector = self._candidate_selector
        if selector is None:
            LOG.warning("No candidate selector available for inspect")
            return {"status": "error", "reason": "no_candidate_selector"}

        try:
            result = selector.select()
        except Exception:
            LOG.exception("CandidateSelector.select() raised during inspect")
            return {"status": "error", "reason": "selector_exception"}

        # Global rejections indicate in-progress items or fetch failures
        if result.global_rejections:
            for reason in result.global_rejections:
                if "in-progress" in reason.lower() or "in_progress" in reason.lower():
                    # Convert candidates back to raw dicts for the items list
                    items = (
                        [c.raw for c in result.candidates] if result.candidates else []
                    )
                    return {"status": "in_progress", "items": items}
            # Other global rejection (e.g. fetch failure)
            return {
                "status": "error",
                "reason": "; ".join(result.global_rejections),
            }

        if result.selected is None:
            return {"status": "idle_no_candidate", "payload": None}

        return {
            "status": "idle_with_candidate",
            "candidate": result.selected.raw,
            "candidate_id": result.selected.id,
            "candidate_title": result.selected.title,
        }

    # -- run idle delegation (engine wrapper) -------------------------------

    def run_idle_delegation(
        self, *, audit_only: bool, spec: Any = None
    ) -> Dict[str, Any]:
        """Attempt to dispatch work when agents are idle.

        Delegates all candidate selection, invariant evaluation, state
        transitions, and dispatch to the engine.  Converts the EngineResult
        back into the dict format expected by callers.

        Returns a dict with at least the following keys:
        - note: human-readable summary
        - dispatched: bool (True if a delegation was dispatched)
        - rejected: list of rejected candidate summaries (may be empty)
        - idle_notification_sent: bool (True if a detailed idle notification was posted)
        - delegate_info: optional dict with dispatch details when dispatched
        """
        assert self.engine is not None  # guaranteed by caller

        if audit_only:
            return {
                "note": "Delegation: skipped (audit_only)",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
            }

        try:
            result = self.engine.process_delegation()
        except Exception:
            LOG.exception("Engine process_delegation raised an exception")
            return {
                "note": "Delegation: engine error",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
                "error": "engine exception",
            }

        # Convert EngineResult to dict format.
        status = result.status

        if status == EngineStatus.SUCCESS:
            action = result.action or "unknown"
            wid = result.work_item_id or "?"
            delegate_title = ""
            if result.candidate_result and result.candidate_result.selected:
                delegate_title = result.candidate_result.selected.title
            delegate_info: Dict[str, Any] = {
                "action": action,
                "id": wid,
                "title": delegate_title,
                "stdout": "",
                "stderr": "",
            }
            if result.dispatch_result:
                delegate_info["pid"] = result.dispatch_result.pid
                delegate_info["container_id"] = result.dispatch_result.container_id
            return {
                "note": f"Delegation: dispatched {action} {wid}",
                "dispatched": True,
                "delegate_info": delegate_info,
                "rejected": self._engine_rejections(result),
                "idle_notification_sent": False,
            }

        if status == EngineStatus.NO_CANDIDATES:
            # The engine already sent a Discord notification via its notifier.
            return {
                "note": "Delegation: skipped (no wl next candidates)",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_notification_sent": True,
            }

        if status == EngineStatus.SKIPPED:
            return {
                "note": f"Delegation: skipped ({result.reason})",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
            }

        if status in (
            EngineStatus.REJECTED,
            EngineStatus.INVARIANT_FAILED,
        ):
            return {
                "note": f"Delegation: blocked ({result.reason})",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_notification_sent": False,
            }

        if status == EngineStatus.DISPATCH_FAILED:
            return {
                "note": f"Delegation: failed ({result.reason})",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_notification_sent": False,
                "error": result.reason,
            }

        # ERROR or any other unexpected status
        return {
            "note": f"Delegation: engine error ({result.reason})",
            "dispatched": False,
            "rejected": self._engine_rejections(result),
            "idle_notification_sent": False,
            "error": result.reason,
        }

    @staticmethod
    def _engine_rejections(result: EngineResult) -> List[Dict[str, str]]:
        """Extract rejected-candidate summaries from an EngineResult for
        backward compatibility with the legacy delegation dict format."""
        rejected: List[Dict[str, str]] = []
        cr = getattr(result, "candidate_result", None)
        if cr is None:
            return rejected
        for rej in getattr(cr, "rejections", ()):
            c = getattr(rej, "candidate", None)
            rejected.append(
                {
                    "id": getattr(c, "id", "?") if c else "?",
                    "title": getattr(c, "title", "(unknown)") if c else "(unknown)",
                    "reason": getattr(rej, "reason", "rejected"),
                }
            )
        return rejected

    # -- delegation report generation ---------------------------------------

    def run_delegation_report(self, spec: Any = None) -> Optional[str]:
        """Generate a human-readable delegation report.

        Queries ``wl in_progress`` and candidate selection to build a report
        suitable for operator display or Discord posting.
        """

        def _call(cmd: str) -> subprocess.CompletedProcess:
            LOG.debug("Running shell (delegation): %s", cmd)
            return self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )

        in_progress_text = ""
        proc = _call("wl in_progress")
        if proc.stdout:
            in_progress_text += proc.stdout
        if proc.stderr and not in_progress_text:
            in_progress_text += proc.stderr
        
        in_progress_text = _normalize_in_progress_output(in_progress_text)

        selection = self._selection_module
        if selection is None:
            try:
                from . import selection
            except Exception:
                import ampa.selection as selection  # type: ignore[no-redef]

        # Load the cross-cycle hash cache from scheduler state so that
        # identical candidates returned on separate wl next calls are
        # suppressed across polling cycles.
        hash_cache = None
        try:
            from .selection import CandidateHashCache

            cache_data = self.store.get_candidate_hash_cache()
            hash_cache = CandidateHashCache.from_dict(cache_data)
        except Exception:
            LOG.debug("Failed to load candidate hash cache; skipping cross-cycle dedup")

        candidates, _payload = selection.fetch_candidates(
            run_shell=self.run_shell,
            command_cwd=self.command_cwd,
            hash_cache=hash_cache,
        )

        # Persist the updated cache so the next poll cycle can use it.
        if hash_cache is not None:
            try:
                self.store.update_candidate_hash_cache(hash_cache.to_dict())
            except Exception:
                LOG.debug("Failed to save candidate hash cache")
        top_candidate = candidates[0] if candidates else None

        report = _build_delegation_report(
            in_progress_output=_trim_text(in_progress_text),
            candidates=candidates,
            top_candidate=top_candidate,
        )
        return report

    # -- stale delegation watchdog ------------------------------------------

    def recover_stale_delegations(self) -> List[Dict[str, Any]]:
        """Detect work items stuck in ``delegated`` stage and reset them.

        When an ``opencode run`` agent process crashes or hangs without
        updating the work item, the item remains in
        ``(in_progress, delegated)`` forever, blocking all future
        delegations via the ``no_in_progress_items`` invariant.

        This method:

        1. Queries ``wl in_progress --json`` for items with
           ``stage == "delegated"``.
        2. Checks each item's ``updatedAt`` against
           ``AMPA_STALE_DELEGATION_THRESHOLD_SECONDS`` (default 7200s).
        3. Resets stale items to ``(open, plan_complete)`` so delegation
           can be retried on the next cycle.
        4. Posts a ``wl comment`` documenting the recovery.
        5. Sends a Discord notification.

        Returns a list of dicts describing recovered items (empty list
        when nothing was stale).
        """
        try:
            thresh_raw = os.getenv("AMPA_STALE_DELEGATION_THRESHOLD_SECONDS", "7200")
            try:
                threshold = int(thresh_raw)
            except Exception:
                threshold = 7200
        except Exception:
            threshold = 7200

        now = _utc_now()
        recovered: List[Dict[str, Any]] = []

        # 1. Query in-progress items
        try:
            proc = self.run_shell(
                "wl in_progress --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception("Stale delegation watchdog: wl in_progress failed")
            return recovered

        if proc.returncode != 0:
            LOG.warning(
                "Stale delegation watchdog: wl in_progress returned rc=%s",
                proc.returncode,
            )
            return recovered

        # Parse items
        try:
            raw = json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Stale delegation watchdog: failed to parse wl in_progress")
            return recovered

        items: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            items = [it for it in raw if isinstance(it, dict)]
        elif isinstance(raw, dict):
            for key in ("workItems", "work_items", "items", "data"):
                val = raw.get(key)
                if isinstance(val, list):
                    items = [it for it in val if isinstance(it, dict)]
                    break

        # 2. Filter to delegated items that are stale
        for item in items:
            try:
                stage = item.get("stage") or item.get("currentStage") or ""
                if stage != "delegated":
                    continue

                work_id = (
                    item.get("id") or item.get("work_item_id") or item.get("work_item")
                )
                if not work_id:
                    continue

                # Determine age from updatedAt
                updated_str = None
                for k in (
                    "updated_at",
                    "updatedAt",
                    "last_updated_at",
                    "updated_ts",
                    "updated",
                ):
                    v = item.get(k)
                    if v:
                        updated_str = v
                        break

                updated_dt = _from_iso(updated_str) if updated_str else None
                age_s = (
                    int((now - updated_dt).total_seconds())
                    if updated_dt is not None
                    else None
                )

                if age_s is None or age_s <= threshold:
                    LOG.debug(
                        "Stale delegation watchdog: %s age=%s <= threshold=%s, skipping",
                        work_id,
                        age_s,
                        threshold,
                    )
                    continue

                # 3. Reset the work item to (open, plan_complete)
                LOG.warning(
                    "Stale delegation watchdog: recovering %s (age=%ss, threshold=%ss)",
                    work_id,
                    age_s,
                    threshold,
                )
                title = item.get("title", "(unknown)")
                reset_proc = self.run_shell(
                    f"wl update {work_id} --status open --stage plan_complete --json",
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                )
                if reset_proc.returncode != 0:
                    LOG.error(
                        "Stale delegation watchdog: failed to reset %s rc=%s stderr=%r",
                        work_id,
                        reset_proc.returncode,
                        (reset_proc.stderr or "")[:512],
                    )
                    continue

                # 4. Post a wl comment documenting the recovery
                comment_text = (
                    f"[watchdog] Stale delegation recovery: this item was stuck in "
                    f"(in_progress, delegated) for {age_s}s "
                    f"(threshold: {threshold}s). "
                    f"Reset to (open, plan_complete) for re-delegation. "
                    f"The previous agent session likely crashed or hung "
                    f"without updating the work item."
                )
                try:
                    self.run_shell(
                        f'wl comment add {work_id} --comment "{comment_text}" '
                        f"--author ampa-watchdog --json",
                        shell=True,
                        check=False,
                        capture_output=True,
                        text=True,
                        cwd=self.command_cwd,
                    )
                except Exception:
                    LOG.exception(
                        "Stale delegation watchdog: failed to post comment on %s",
                        work_id,
                    )

                recovery_info = {
                    "work_item_id": str(work_id),
                    "title": title,
                    "age_seconds": age_s,
                    "threshold_seconds": threshold,
                    "reset_to": "open/plan_complete",
                }
                recovered.append(recovery_info)
                LOG.info(
                    "Stale delegation watchdog: recovered %s (%s)",
                    work_id,
                    title,
                )

            except Exception:
                LOG.exception(
                    "Stale delegation watchdog: error processing item %s",
                    item.get("id", "?"),
                )

        # 5. Discord notification (batched)
        if recovered:
            try:
                notif = self._notifications_module
                if notif is not None:
                    lines = [
                        f"Stale delegation watchdog recovered {len(recovered)} item(s):"
                    ]
                    for info in recovered:
                        lines.append(
                            f"- {info['work_item_id']}: {info['title']} "
                            f"(stale {info['age_seconds']}s, reset to {info['reset_to']})"
                        )
                    msg = "\n".join(lines)
                    notif.notify(
                        "Stale Delegation Recovery",
                        msg,
                        message_type="warning",
                    )
            except Exception:
                LOG.exception(
                    "Stale delegation watchdog: failed to send Discord notification"
                )

        return recovered

    # -- main delegation flow -----------------------------------------------

    def execute(self, spec: Any, run: Any, output: Optional[str]) -> Any:
        """Run the full delegation flow for a delegation CommandSpec.

        This replaces the ``if spec.command_type == "delegation":`` branch
        that was formerly in ``Scheduler.start_command()``.

        Parameters
        ----------
        spec : CommandSpec
            The delegation command spec.
        run : RunResult
            The RunResult produced by the executor.
        output : str or None
            Raw output captured from the executor.

        Returns
        -------
        RunResult
            An updated RunResult with delegation metadata attached.
        """
        notif = self._notifications_module

        # Determine effective audit-only behaviour.  If an operator has set
        # an explicit `auto_assign_enabled` flag in the delegation command
        # metadata, treat that as authoritative (present -> controls live
        # promotions).  Otherwise fall back to the legacy `audit_only`
        # metadata for backwards compatibility.
        meta = spec.metadata if isinstance(spec.metadata, dict) else {}
        if "auto_assign_enabled" in meta:
            # auto_assign_enabled == True -> live promotions allowed
            audit_only_effective = not _bool_meta(meta.get("auto_assign_enabled"))
            LOG.info(
                "Handling delegation command: %s (auto_assign_enabled=%s -> audit_only=%s)",
                spec.command_id,
                _bool_meta(meta.get("auto_assign_enabled")),
                audit_only_effective,
            )
        else:
            audit_only_effective = _bool_meta(meta.get("audit_only"))
            LOG.info(
                "Handling delegation command: %s (audit_only=%s)",
                spec.command_id,
                _bool_meta(meta.get("audit_only")),
            )
        # Inspect current state first. If there is a candidate that will be
        # dispatched we want to avoid sending the pre-dispatch report to
        # Discord (otherwise operators see two nearly-identical messages).
        # Use the computed effective audit-only value for gating
        audit_only = audit_only_effective
        inspect = self._inspect_idle_delegation()
        status = inspect.get("status")

        # Only generate and send a pre-dispatch report when we are not
        # about to dispatch a candidate. If we will dispatch (status
        # == 'idle_with_candidate' and audit_only is false) skip the
        # pre-report; a post-dispatch report will be sent after the
        # delegation completes.
        report = None
        sent_pre_report = False
        if audit_only or status != "idle_with_candidate":
            try:
                LOG.info(
                    "Generating pre-dispatch delegation report for %s",
                    spec.command_id,
                )
                report = self.run_delegation_report(spec)
            except Exception:
                LOG.exception("Delegation report generation failed")
            if report:
                LOG.info(
                    "Pre-dispatch delegation report generated (len=%d)", len(report)
                )
                output = report
                # A pre-report was generated so the idle-no-candidate
                # fallback notification should not fire regardless of whether
                # the dedup check suppresses this particular send.
                sent_pre_report = True
                try:
                    if self._is_delegation_report_changed(spec.command_id, report):
                        message = _build_delegation_discord_message(report)
                        report_title = (
                            spec.title
                            or spec.metadata.get("discord_label")
                            or "Delegation Report"
                        )
                        if notif is not None:
                            notif.notify(
                                report_title,
                                message,
                                message_type="command",
                            )
                        LOG.info(
                            "Sent pre-dispatch notification for %s", spec.command_id
                        )
                except Exception:
                    LOG.exception("Delegation discord notification failed")
        # if we skipped creating a pre-report, 'report' stays None and
        # 'output' remains as previously (possibly None). Proceed to
        # handling the inspected status below.

        # ensure status variable is available below
        # status may already be set above; if not, extract it
        status = inspect.get("status")
        if status == "in_progress":
            print("There is work in progress and thus no new work will be delegated.")
            LOG.info("Delegation skipped because work is in-progress")
            result = {
                "note": "Delegation: skipped (in_progress items)",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
            }
        elif status == "idle_no_candidate":
            # More descriptive idle message for operators
            idle_msg = "Delegation idle: no candidates returned"
            print(idle_msg)
            LOG.info("Delegation: idle_no_candidate - %s", idle_msg)
            result = {
                "note": "Delegation: skipped (no actionable candidates)",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
            }
            # If we did not already send a detailed pre-report, send a
            # short notification so Discord reflects the idle state.
            if not sent_pre_report:
                try:
                    if self._is_delegation_report_changed(spec.command_id, idle_msg):
                        idle_title = (
                            spec.title
                            or spec.metadata.get("discord_label")
                            or "Delegation Report"
                        )
                        if notif is not None:
                            notif.notify(
                                idle_title,
                                idle_msg,
                                message_type="command",
                            )
                except Exception:
                    LOG.exception("Failed to send idle-state notification")
        elif status == "idle_with_candidate":
            delegate_id = inspect.get("candidate_id")
            delegate_title = inspect.get("candidate_title") or "(no title)"
            print(f"Starting work on: {delegate_title} - {delegate_id or '?'}")
            result = self.run_idle_delegation(audit_only=audit_only, spec=spec)
            # If delegation did not dispatch anything, ensure operators see
            # an idle-state message unless a detailed idle notification was
            # already sent by the delegation routine.
            try:
                note = result.get("note") if isinstance(result, dict) else str(result)
                dispatched = bool(
                    result.get("dispatched") if isinstance(result, dict) else False
                )
                idle_notification_sent = bool(
                    result.get("idle_notification_sent")
                    if isinstance(result, dict)
                    else False
                )
                if not dispatched:
                    # Build an idle message that includes the actual reason
                    # delegation was skipped so operators can diagnose without
                    # checking logs.
                    rejection_details: List[str] = []
                    if isinstance(result, dict):
                        if result.get("rejected"):
                            for rej in result["rejected"]:
                                rej_id = rej.get("id", "?")
                                rej_reason = rej.get("reason", "rejected")
                                rejection_details.append(f"{rej_id}: {rej_reason}")
                    if note and "blocked" in str(note).lower():
                        idle_msg = str(note)
                    elif note and "skipped" in str(note).lower():
                        idle_msg = str(note)
                    elif rejection_details:
                        idle_msg = (
                            "Delegation skipped: all candidates rejected\n"
                            + "\n".join(f"── {d}" for d in rejection_details)
                        )
                    else:
                        idle_msg = "Agents are idle: no actionable items found"
                    print(idle_msg)
                    # If we didn't already send a detailed pre-report or the
                    # delegation routine didn't post its detailed idle notification,
                    # send a short idle notification so Discord reflects the
                    # current idle state.
                    if not sent_pre_report and not idle_notification_sent:
                        if self._is_delegation_report_changed(
                            spec.command_id, idle_msg
                        ):
                            try:
                                idle_title = (
                                    spec.title
                                    or spec.metadata.get("discord_label")
                                    or "Delegation Report"
                                )
                                if notif is not None:
                                    notif.notify(
                                        idle_title,
                                        idle_msg,
                                        message_type="command",
                                    )
                            except Exception:
                                LOG.exception("Failed to send idle-state notification")
            except Exception:
                LOG.exception("Failed to handle no-actionable-candidates path")
        else:
            print("There is no candidate to delegate.")
            result = {
                "note": "Delegation: skipped (in_progress check failed)",
                "dispatched": False,
                "rejected": [],
                "idle_notification_sent": False,
            }
        # Send a follow-up Discord notification when a delegation action
        # was actually dispatched so the Discord report reflects the
        # resulting state instead of the pre-delegation dry-run.
        try:
            # If something was dispatched, re-run the report to capture
            # the post-dispatch state and post that as an update.
            dispatched_flag = False
            if isinstance(result, dict):
                dispatched_flag = bool(result.get("dispatched"))
            if dispatched_flag:
                try:
                    post_report = self.run_delegation_report(spec)
                    if post_report:
                        # Update the stored hash so the next cycle
                        # compares against this post-dispatch state
                        # rather than the stale pre-dispatch content.
                        self._is_delegation_report_changed(spec.command_id, post_report)
                        post_message = _build_delegation_discord_message(post_report)
                        post_title = (
                            spec.title
                            or spec.metadata.get("discord_label")
                            or "Delegation Report"
                        )
                        if notif is not None:
                            notif.notify(
                                post_title,
                                post_message,
                                message_type="command",
                            )
                except Exception:
                    LOG.exception("Failed to send post-delegation notification")
        except Exception:
            LOG.exception("Delegation notification follow-up failed")

        # Use the structured result.note when available
        summary_note = None
        if isinstance(result, dict):
            summary_note = result.get("note")
        else:
            try:
                summary_note = str(result)
            except Exception:
                summary_note = None
        LOG.info("Delegation summary: %s", summary_note)
        # Attach delegation result as metadata on the RunResult so
        # formatters can include the action taken.
        delegation_meta: Dict[str, Any] = {}
        if isinstance(result, dict):
            delegation_meta["delegation"] = result
        if isinstance(run, CommandRunResult):
            run = CommandRunResult(
                start_ts=run.start_ts,
                end_ts=run.end_ts,
                exit_code=run.exit_code,
                output=output or run.output,
                metadata=delegation_meta or None,
            )
        else:
            run = RunResult(
                start_ts=run.start_ts,
                end_ts=run.end_ts,
                exit_code=run.exit_code,
                metadata=delegation_meta or None,
            )
        return run
