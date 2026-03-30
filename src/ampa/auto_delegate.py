"""Auto-delegate scheduler command.

Periodically runs ``wl next --json`` and automatically delegates the
recommended work item to the GitHub Copilot coding agent when the item is in
``in_review`` stage and has ``high`` or ``critical`` priority.

Retry / back-off behaviour
--------------------------
If ``wl gh delegate`` fails the command retries up to ``max_retries`` times
(default 3) with exponential back-off.  After all retries are exhausted a
Discord notification is posted containing the error and the work-item id.

Configuration
-------------
All behaviour can be driven from the ``CommandSpec.metadata`` dict:

* ``max_retries`` (int, default 3) — number of ``wl gh delegate`` retries.
* ``retry_backoff_base_seconds`` (float, default 2.0) — base delay for
  exponential back-off.  Actual delay for attempt *n* (0-indexed) is
  ``base * 2^n`` seconds.
* ``eligible_stages`` (list[str]) — stages that qualify for delegation
  (default ``["in_review"]``).
* ``eligible_priorities`` (list[str]) — priorities that qualify
  (default ``["high", "critical"]``).

The command does **not** require LLM availability.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("ampa.auto_delegate")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_ELIGIBLE_STAGES: List[str] = ["in_review"]
_DEFAULT_ELIGIBLE_PRIORITIES: List[str] = ["high", "critical"]
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BACKOFF_BASE: float = 2.0
_GITHUB_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+")


# ---------------------------------------------------------------------------
# AutoDelegateRunner
# ---------------------------------------------------------------------------


class AutoDelegateRunner:
    """Run the auto-delegate scheduler command.

    Parameters
    ----------
    run_shell:
        Callable with the same signature as :func:`subprocess.run`.  Injected
        by the scheduler for testability.
    command_cwd:
        Working directory for shell commands.
    notifier:
        Object with a ``notify(title, body, message_type)`` method used for
        Discord notifications.  When *None* the built-in
        ``ampa.notifications.notify`` function is used.
    sleep_fn:
        Callable used to sleep between retries.  Injected in tests to avoid
        real waits.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        notifier: Optional[Any] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self._notifier = notifier
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, spec: Any) -> Dict[str, Any]:
        """Execute one auto-delegate cycle.

        1. Call ``wl next --json`` to get the recommended work item.
        2. Check whether stage and priority qualify for delegation.
        3. If they do, run ``wl gh delegate <id>`` with retry/back-off.
        4. Return a result dict describing the outcome.

        Parameters
        ----------
        spec:
            The :class:`~ampa.scheduler_types.CommandSpec` for the running
            command.  Metadata keys control eligibility and retry behaviour.

        Returns
        -------
        dict
            Keys:
            * ``action`` — one of ``"delegated"``, ``"skipped"``,
              ``"no_candidate"``, ``"delegate_failed"``, ``"query_failed"``
            * ``work_item_id`` — the id of the candidate (or ``None``)
            * ``note`` — human-readable summary
            * ``retries`` — number of retry attempts made (only present when
              a delegation was attempted)
        """
        metadata: Dict[str, Any] = getattr(spec, "metadata", {}) or {}
        eligible_stages = _coerce_list(
            metadata.get("eligible_stages"), _DEFAULT_ELIGIBLE_STAGES
        )
        eligible_priorities = _coerce_list(
            metadata.get("eligible_priorities"), _DEFAULT_ELIGIBLE_PRIORITIES
        )
        try:
            max_retries = int(metadata.get("max_retries", _DEFAULT_MAX_RETRIES))
        except (TypeError, ValueError):
            max_retries = _DEFAULT_MAX_RETRIES
        try:
            backoff_base = float(
                metadata.get("retry_backoff_base_seconds", _DEFAULT_BACKOFF_BASE)
            )
        except (TypeError, ValueError):
            backoff_base = _DEFAULT_BACKOFF_BASE

        # 1. Fetch next work item recommendation
        candidate = self._fetch_next()
        if candidate is None:
            LOG.info("auto-delegate: wl next returned no candidate")
            return {
                "action": "no_candidate",
                "work_item_id": None,
                "note": "wl next returned no candidate",
            }
        if isinstance(candidate, str) and candidate == "__query_failed__":
            LOG.warning("auto-delegate: wl next query failed")
            return {
                "action": "query_failed",
                "work_item_id": None,
                "note": "wl next query failed",
            }

        if isinstance(candidate, dict):
            candidate_dict: Dict[str, Any] = candidate
        else:
            candidate_dict = {}
        work_item_id = _extract_id(candidate_dict)
        stage = _extract_stage(candidate_dict)
        priority = _extract_priority(candidate_dict)
        title = candidate_dict.get("title") or str(work_item_id)

        LOG.info(
            "auto-delegate: candidate id=%s stage=%r priority=%r title=%r",
            work_item_id,
            stage,
            priority,
            title,
        )

        # 2. Eligibility check
        if stage not in eligible_stages or priority not in eligible_priorities:
            note = (
                f"auto-delegate: skipping {work_item_id!r} "
                f"(stage={stage!r}, priority={priority!r}) — "
                f"not in eligible stages={eligible_stages!r} / "
                f"priorities={eligible_priorities!r}"
            )
            LOG.info(note)
            return {
                "action": "skipped",
                "work_item_id": work_item_id,
                "note": note,
            }

        # 3. Delegate with retry / back-off
        last_error: Optional[str] = None
        for attempt in range(max_retries):
            if attempt > 0:
                delay = backoff_base * (2 ** (attempt - 1))
                LOG.info(
                    "auto-delegate: retry %d/%d for %s in %.1fs",
                    attempt,
                    max_retries - 1,
                    work_item_id,
                    delay,
                )
                self._sleep(delay)

            success, error, stdout, stderr = self._delegate(work_item_id)
            if success:
                note = f"auto-delegate: delegated {work_item_id!r} ({title!r})"
                LOG.info(note)
                github_url = _extract_github_url(stdout) or _extract_github_url(stderr)
                self._notify_success(
                    work_item_id=work_item_id,
                    title=title,
                    stage=stage,
                    priority=priority,
                    destination_url=github_url,
                )
                return {
                    "action": "delegated",
                    "work_item_id": work_item_id,
                    "note": note,
                    "retries": attempt,
                    "github_url": github_url,
                }
            last_error = error
            LOG.warning(
                "auto-delegate: wl gh delegate failed (attempt %d/%d) for %s: %s",
                attempt + 1,
                max_retries,
                work_item_id,
                last_error,
            )

        # All retries exhausted — send Discord notification
        error_note = (
            f"auto-delegate: failed to delegate {work_item_id!r} ({title!r}) "
            f"after {max_retries} attempt(s): {last_error}"
        )
        LOG.error(error_note)
        self._notify_failure(work_item_id, title, last_error, max_retries)
        return {
            "action": "delegate_failed",
            "work_item_id": work_item_id,
            "note": error_note,
            "retries": max_retries,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_next(self) -> Optional[Any]:
        """Run ``wl next --json`` and return the top candidate dict.

        Returns ``None`` when there are no candidates and the sentinel string
        ``"__query_failed__"`` when the command itself fails.
        """
        try:
            proc = self.run_shell(
                "wl next --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception("auto-delegate: exception running wl next")
            return "__query_failed__"

        if proc.returncode != 0:
            LOG.warning(
                "auto-delegate: wl next rc=%s stderr=%r",
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return "__query_failed__"

        stdout = (proc.stdout or "").strip()
        if not stdout:
            return None

        try:
            payload = json.loads(stdout)
        except Exception:
            LOG.warning(
                "auto-delegate: wl next returned invalid JSON: %r", stdout[:512]
            )
            return "__query_failed__"

        candidates = _normalize_candidates(payload)
        return candidates[0] if candidates else None

    def _delegate(
        self, work_item_id: str
    ) -> Tuple[bool, Optional[str], str, str]:
        """Run ``wl gh delegate <id>`` and return ``(success, error_message, stdout, stderr)``."""
        try:
            proc = self.run_shell(
                f"wl gh delegate {work_item_id}",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception as exc:
            return False, str(exc), "", ""

        if proc.returncode == 0:
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            return True, None, stdout, stderr
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        error = stderr or stdout or f"exit code {proc.returncode}"
        return False, error, stdout, stderr

    def _notify_failure(
        self,
        work_item_id: str,
        title: str,
        error: Optional[str],
        attempts: int,
    ) -> None:
        """Post a Discord notification about persistent delegation failure."""
        msg_title = f"Auto-delegate failed — {title}"
        msg_body = (
            f"Could not delegate work item **{work_item_id}** after {attempts} "
            f"attempt(s).\nError: {error or '(unknown)'}"
        )
        try:
            if self._notifier is not None:
                self._notifier.notify(
                    title=msg_title,
                    body=msg_body,
                    message_type="error",
                )
            else:
                from . import notifications as _notifications

                _notifications.notify(
                    title=msg_title,
                    body=msg_body,
                    message_type="error",
                )
        except Exception:
            LOG.exception(
                "auto-delegate: failed to send failure notification for %s",
                work_item_id,
            )

    def _notify_success(
        self,
        work_item_id: str,
        title: str,
        stage: str,
        priority: str,
        destination_url: Optional[str],
    ) -> None:
        """Post a Discord notification about a successful delegation."""
        stage_label = stage or "(unknown)"
        priority_label = priority or "(unknown)"
        destination_label = destination_url or "(GitHub URL pending)"
        msg_title = f"Auto-delegate succeeded — {title}"
        msg_body = "\n".join(
            [
                f"Work item **{work_item_id}** delegated successfully.",
                f"Stage: {stage_label}",
                f"Priority: {priority_label}",
                "Action: wl gh delegate",
                f"Destination: {destination_label}",
            ]
        )
        try:
            if self._notifier is not None:
                self._notifier.notify(
                    title=msg_title,
                    body=msg_body,
                    message_type="completion",
                )
            else:
                from . import notifications as _notifications

                _notifications.notify(
                    title=msg_title,
                    body=msg_body,
                    message_type="completion",
                )
        except Exception:
            # Keep behavior consistent with failure notification: log and
            # continue without raising so runner.run() remains resilient.
            LOG.exception(
                "auto-delegate: failed to send success notification for %s",
                work_item_id,
            )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_github_url(text: Optional[str]) -> Optional[str]:
    """Extract the first GitHub URL from *text*, or ``None`` if not found."""
    if not text:
        return None
    match = _GITHUB_URL_RE.search(text)
    return match.group(0) if match else None


def _extract_id(item: Any) -> str:
    if not isinstance(item, dict):
        return "(unknown)"
    for key in ("id", "work_item_id", "workItemId"):
        val = item.get(key)
        if val is not None:
            return str(val)
    return "(unknown)"


def _extract_stage(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("stage", "currentStage", "current_stage"):
        val = item.get(key)
        if val is not None:
            return str(val).lower()
    return ""


def _extract_priority(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    val = item.get("priority")
    if val is not None:
        return str(val).lower()
    return ""


def _coerce_list(value: Any, default: List[str]) -> List[str]:
    """Coerce a metadata value to a list of strings."""
    if value is None:
        return default
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return default


def _normalize_candidates(payload: Any) -> List[Dict[str, Any]]:
    """Extract a flat list of work-item dicts from a ``wl next`` JSON payload."""
    if payload is None:
        return []
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                out.append(item)
        return out
    if not isinstance(payload, dict):
        return []
    for key in ("candidates", "workItems", "work_items", "items", "data", "results"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    # single item at top level
    for key in ("workItem", "work_item", "item"):
        val = payload.get(key)
        if isinstance(val, dict):
            return [val]
    # If the payload itself looks like a work item (has an "id" key), treat it
    # as a single candidate.
    if "id" in payload:
        return [payload]
    return []
