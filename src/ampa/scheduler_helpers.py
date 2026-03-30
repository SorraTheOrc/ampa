"""Scheduler initialization and operational helpers.

Module-level utility functions extracted from the Scheduler class to keep
the main scheduler module focused on scheduling logic.  All functions
operate on a :class:`~ampa.scheduler_store.SchedulerStore` and do not
require Scheduler instance state.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List

from .scheduler_types import CommandSpec, _utc_now, _from_iso
from .scheduler_store import SchedulerStore

LOG = logging.getLogger("ampa.scheduler")

# ---------------------------------------------------------------------------
# Well-known command identifiers for auto-registered built-in commands.
# ---------------------------------------------------------------------------

_WATCHDOG_COMMAND_ID = "stale-delegation-watchdog"
# (removed) _TEST_BUTTON_COMMAND_ID was intentionally deleted — test-button
# command has been removed from the codebase.
_AUTO_DELEGATE_COMMAND_ID = "auto-delegate"
_PR_MONITOR_COMMAND_ID = "pr-monitor"
_AUDIT_COMMAND_ID = "wl-audit"


# ---------------------------------------------------------------------------
# Initialization helpers (called from Scheduler.__init__)
# ---------------------------------------------------------------------------


def clear_stale_running_states(store: SchedulerStore) -> None:
    """Clear ``running`` flags for commands whose last_start_ts is older
    than ``AMPA_STALE_RUNNING_THRESHOLD_SECONDS`` (default 3600s).

    This prevents commands from remaining marked as running due to a
    previous crash or unhandled exception which would otherwise block
    future scheduling.
    """
    try:
        thresh_raw = os.getenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "3600")
        try:
            threshold = int(thresh_raw)
        except Exception:
            threshold = 3600
        now = _utc_now()
        for cmd in store.list_commands():
            try:
                st = store.get_state(cmd.command_id) or {}
                if st.get("running") is not True:
                    continue
                last_start_iso = st.get("last_start_ts")
                last_start = _from_iso(last_start_iso) if last_start_iso else None
                age = (
                    None
                    if last_start is None
                    else int((now - last_start).total_seconds())
                )
                if age is None or age > threshold:
                    st["running"] = False
                    store.update_state(cmd.command_id, st)
                    LOG.info(
                        "Cleared stale running flag for %s (age_s=%s)",
                        cmd.command_id,
                        age,
                    )
            except Exception:
                LOG.exception(
                    "Failed to evaluate/clear running state for %s",
                    getattr(cmd, "command_id", "?"),
                )
    except Exception:
        LOG.exception("Unexpected error while clearing stale running states")


def ensure_watchdog_command(store: SchedulerStore) -> None:
    """Register the stale-delegation-watchdog command if absent."""
    try:
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _WATCHDOG_COMMAND_ID:
                LOG.debug(
                    "Watchdog command already registered: %s", _WATCHDOG_COMMAND_ID
                )
                return
        watchdog_spec = CommandSpec(
            command_id=_WATCHDOG_COMMAND_ID,
            command="echo watchdog",
            requires_llm=False,
            frequency_minutes=30,
            priority=0,
            metadata={},
            title="Stale Delegation Watchdog",
            max_runtime_minutes=5,
            command_type="stale-delegation-watchdog",
        )
        store.add_command(watchdog_spec)
        LOG.info(
            "Auto-registered watchdog command: %s (every %dm)",
            _WATCHDOG_COMMAND_ID,
            watchdog_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register watchdog command")


# ensure_test_button_command was removed along with the test-button
# feature. No code path should call this function and any historical
# artifacts have been removed.


def ensure_auto_delegate_command(store: SchedulerStore) -> None:
    """Register the auto-delegate command if absent.

    The command is **disabled by default** (``metadata.enabled = False``)
    so operators must opt-in via their store configuration.
    """
    try:
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _AUTO_DELEGATE_COMMAND_ID:
                LOG.debug(
                    "Auto-delegate command already registered: %s",
                    _AUTO_DELEGATE_COMMAND_ID,
                )
                return
        auto_delegate_spec = CommandSpec(
            command_id=_AUTO_DELEGATE_COMMAND_ID,
            command="echo auto-delegate",
            requires_llm=False,
            frequency_minutes=30,
            priority=0,
            metadata={"enabled": False},
            title="Auto Delegate",
            max_runtime_minutes=5,
            command_type="auto-delegate",
        )
        store.add_command(auto_delegate_spec)
        LOG.info(
            "Auto-registered auto-delegate command: %s (every %dm, disabled by default)",
            _AUTO_DELEGATE_COMMAND_ID,
            auto_delegate_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register auto-delegate command")


def ensure_pr_monitor_command(store: SchedulerStore) -> None:
    """Register the PR monitor command if absent.

    Runs hourly (``frequency_minutes=60``) and scans open pull requests for
    CI status.  Requires ``gh`` CLI availability at runtime.
    """
    try:
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _PR_MONITOR_COMMAND_ID:
                LOG.debug(
                    "PR monitor command already registered: %s",
                    _PR_MONITOR_COMMAND_ID,
                )
                return
        pr_monitor_spec = CommandSpec(
            command_id=_PR_MONITOR_COMMAND_ID,
            command="echo pr-monitor",
            requires_llm=False,
            frequency_minutes=60,
            priority=0,
            metadata={"dedup": True, "max_prs": 50, "auto_review": True},
            title="PR Monitor",
            max_runtime_minutes=10,
            command_type="pr-monitor",
        )
        store.add_command(pr_monitor_spec)
        LOG.info(
            "Auto-registered PR monitor command: %s (every %dm)",
            _PR_MONITOR_COMMAND_ID,
            pr_monitor_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register PR monitor command")


def ensure_audit_command(store: SchedulerStore) -> None:
    """Register the audit poller command if absent.

    The audit poller selects in-review work items and runs the descriptor-
    driven audit handlers. It is safe to auto-register with a conservative
    default; operators may customize or remove the entry in their
    project-local scheduler_store.json.
    """
    try:
        # Avoid auto-registering the audit command for in-memory/test stores
        # (e.g. unit tests set store.path = ":memory:"). Operators should
        # have an explicit entry in their per-project scheduler_store.json
        # when deploying; only auto-register for real stores.
        store_path = getattr(store, "path", None)
        if store_path == ":memory:":
            LOG.debug("Skipping audit auto-registration for in-memory store")
            return
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _AUDIT_COMMAND_ID:
                LOG.debug("Audit command already registered: %s", _AUDIT_COMMAND_ID)
                return
        audit_spec = CommandSpec(
            command_id=_AUDIT_COMMAND_ID,
            command="true",
            requires_llm=False,
            frequency_minutes=2,
            priority=0,
            metadata={
                "discord_label": "wl audit",
                "audit_cooldown_hours": 6,
                "audit_cooldown_hours_in_review": 1,
                "truncate_chars": 65536,
            },
            title="Audit",
            max_runtime_minutes=5,
            command_type="audit",
        )
        store.add_command(audit_spec)
        LOG.info(
            "Auto-registered audit command: %s (every %dm)",
            _AUDIT_COMMAND_ID,
            audit_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register audit command")


# send_test_button_message removed — test-button feature removed.


def log_health(store: SchedulerStore) -> None:
    """Emit a periodic health report about scheduled commands."""
    try:
        cmds = store.list_commands()
    except Exception:
        LOG.exception("Failed to read commands for health report")
        return
    lines: List[str] = []
    now = _utc_now()
    for cmd in cmds:
        try:
            state = store.get_state(cmd.command_id) or {}
            last_run_iso = state.get("last_run_ts")
            last_run_dt = _from_iso(last_run_iso) if last_run_iso else None
            age = (
                int((now - last_run_dt).total_seconds())
                if last_run_dt is not None
                else None
            )
            running = bool(state.get("running"))
            last_exit = state.get("last_exit_code")
            lines.append(
                f"{cmd.command_id} title={cmd.title!r} last_run={last_run_iso or 'never'} "
                f"age_s={age if age is not None else 'NA'} exit={last_exit} running={running}"
            )
        except Exception:
            LOG.exception(
                "Failed to build health line for %s",
                getattr(cmd, "command_id", "?"),
            )
    LOG.info("Scheduler health report: %d commands\n%s", len(lines), "\n".join(lines))
