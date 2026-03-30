"""Adapter classes bridging scheduler infrastructure to engine protocols.

These adapters wrap the scheduler's existing ``run_shell`` subprocess runner,
``SchedulerStore``, and Discord notification infrastructure so the engine's
protocol-based dependencies can be satisfied without duplicating logic.

Usage::

    from ampa.engine.adapters import (
        ShellCandidateFetcher,
        ShellInProgressQuerier,
        ShellWorkItemFetcher,
        ShellWorkItemUpdater,
        ShellCommentWriter,
        StoreDispatchRecorder,
        DiscordNotificationSender,
    )
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("ampa.engine.adapters")


# ---------------------------------------------------------------------------
# Shell-based adapters (wrap run_shell for wl CLI calls)
# ---------------------------------------------------------------------------


class ShellCandidateFetcher:
    """Fetches candidates from ``wl next --json`` via a shell runner.

    Satisfies :class:`ampa.engine.candidates.CandidateFetcher`.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self._run_shell = run_shell
        self._cwd = command_cwd
        self._timeout = timeout

    def fetch(self) -> list[dict[str, Any]]:
        """Return raw candidate dicts from ``wl next --json``."""
        try:
            proc = self._run_shell(
                "wl next --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=self._timeout,
            )
        except Exception:
            LOG.exception("wl next --json failed")
            return []

        if proc.returncode != 0:
            LOG.warning(
                "wl next --json returned rc=%s stderr=%r",
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return []

        try:
            raw = json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl next output")
            return []

        return self._normalize(raw)

    @staticmethod
    def _normalize(payload: Any) -> list[dict[str, Any]]:
        """Extract a list of candidate dicts from various wl next formats."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            # Nested format: {"workItems": [...]} or {"items": [...]}
            for key in ("workItems", "work_items", "items", "data", "recommendations"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
            # Single recommendation format: {"workItem": {...}}
            wi = payload.get("workItem") or payload.get("work_item")
            if isinstance(wi, dict):
                return [wi]
        return []


class ShellInProgressQuerier:
    """Queries in-progress item count via ``wl in_progress --json``.

    Satisfies :class:`ampa.engine.candidates.InProgressQuerier`.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self._run_shell = run_shell
        self._cwd = command_cwd
        self._timeout = timeout

    def count_in_progress(self) -> int:
        """Return the count of in-progress work items."""
        try:
            proc = self._run_shell(
                "wl in_progress --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=self._timeout,
            )
        except Exception:
            LOG.exception("wl in_progress --json failed")
            return -1  # signal failure; selector treats -1 as unknown

        if proc.returncode != 0:
            LOG.warning(
                "wl in_progress --json returned rc=%s stderr=%r",
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            # Retry once
            try:
                proc = self._run_shell(
                    "wl in_progress --json",
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self._cwd,
                    timeout=self._timeout,
                )
            except Exception:
                LOG.exception("wl in_progress --json retry failed")
                return -1

            if proc.returncode != 0:
                return -1

        items = self._parse(proc.stdout)
        return len(items) if items is not None else -1

    @staticmethod
    def _parse(stdout: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Parse wl in_progress output into a list of work item dicts."""
        try:
            raw = json.loads(stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl in_progress output")
            return None

        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            for key in ("workItems", "work_items", "items", "data"):
                val = raw.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
        return []


class ShellWorkItemFetcher:
    """Fetches full work item data via ``wl show <id> --children --json``.

    Satisfies :class:`ampa.engine.core.WorkItemFetcher`.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self._run_shell = run_shell
        self._cwd = command_cwd
        self._timeout = timeout

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        """Fetch ``wl show {id} --children --json`` output."""
        try:
            proc = self._run_shell(
                f"wl show {work_item_id} --children --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=self._timeout,
            )
        except Exception:
            LOG.exception("wl show %s failed", work_item_id)
            return None

        if proc.returncode != 0:
            LOG.warning(
                "wl show %s returned rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return None

        try:
            return json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl show output for %s", work_item_id)
            return None


class ShellWorkItemUpdater:
    """Applies state transitions via ``wl update``.

    Satisfies :class:`ampa.engine.core.WorkItemUpdater`.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self._run_shell = run_shell
        self._cwd = command_cwd
        self._timeout = timeout

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        """Run ``wl update`` with the given fields. Returns True on success."""
        parts = [f"wl update {work_item_id}"]
        if status:
            parts.append(f"--status {status}")
        if stage:
            parts.append(f"--stage {stage}")
        if assignee:
            parts.append(f"--assignee {assignee}")
        parts.append("--json")
        cmd = " ".join(parts)

        try:
            proc = self._run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=self._timeout,
            )
        except Exception:
            LOG.exception("wl update %s failed", work_item_id)
            return False

        if proc.returncode != 0:
            LOG.warning(
                "wl update %s returned rc=%s stderr=%r",
                work_item_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return False

        return True


class ShellCommentWriter:
    """Writes comments via ``wl comment add``.

    Satisfies :class:`ampa.engine.core.WorkItemCommentWriter`.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        self._run_shell = run_shell
        self._cwd = command_cwd
        self._timeout = timeout

    def write_comment(
        self,
        work_item_id: str,
        comment: str,
        author: str = "ampa-engine",
    ) -> bool:
        """Run ``wl comment add`` and return True on success."""
        # Escape double quotes in the comment for shell safety
        safe_comment = comment.replace('"', '\\"')
        cmd = f'wl comment add {work_item_id} --comment "{safe_comment}" --author {author} --json'

        try:
            proc = self._run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                timeout=self._timeout,
            )
        except Exception:
            LOG.exception("wl comment add %s failed", work_item_id)
            return False

        return proc.returncode == 0


# ---------------------------------------------------------------------------
# Store adapter
# ---------------------------------------------------------------------------


class StoreDispatchRecorder:
    """Records dispatches to the SchedulerStore.

    Satisfies :class:`ampa.engine.core.DispatchRecorder`.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def record_dispatch(self, record: dict[str, Any]) -> str | None:
        """Append a dispatch record and return the generated ID."""
        try:
            return self._store.append_dispatch(record)
        except Exception:
            LOG.exception("Failed to persist dispatch record")
            return None


# ---------------------------------------------------------------------------
# Discord notification adapter
# ---------------------------------------------------------------------------


class DiscordNotificationSender:
    """Sends notifications via the notification API.

    Satisfies :class:`ampa.engine.core.NotificationSender`.

    Parameters
    ----------
    hostname:
        Machine hostname for message context.
    """

    def __init__(
        self,
        hostname: str | None = None,
    ) -> None:
        self._hostname = hostname or _safe_hostname()

    def send(
        self,
        message: str,
        *,
        title: str = "",
        level: str = "info",
    ) -> bool:
        """Send a Discord notification. Returns True on success."""
        try:
            from ampa import notifications as notif_mod

            return notif_mod.notify(
                title or "AMPA Engine",
                message,
                message_type="engine",
            )
        except Exception:
            LOG.exception("Failed to send Discord notification")
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_hostname() -> str:
    """Return the machine hostname, or a fallback."""
    try:
        return os.uname().nodename
    except Exception:
        return "unknown"


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
