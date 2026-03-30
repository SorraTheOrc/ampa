"""Tests for the stale delegation watchdog (_recover_stale_delegations).

Work item: SA-0MLXJCNMR0OQD7Z5
Parent: SA-0MLWQI60Q0CJGFO2 (engine core)

These tests verify that:
1. The watchdog detects work items stuck in (in_progress, delegated) state
   beyond the configured threshold.
2. Stale items are reset to (open, plan_complete) for re-delegation.
3. A wl comment is posted documenting the recovery action.
4. A Discord notification is sent when recovery occurs.
5. Items below the threshold are not recovered.
6. The watchdog is a no-op when no delegated items exist.
7. The watchdog handles errors gracefully (wl failures, parse errors).
8. The watchdog runs as its own scheduled command (command_type
   'stale-delegation-watchdog'), not embedded in the delegation flow.
9. The watchdog command is auto-registered at scheduler init.
10. The AMPA_STALE_DELEGATION_THRESHOLD_SECONDS env var is respected.
"""

import datetime as dt
import json
import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    RunResult,
    _utc_now,
    _to_iso,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_types import SchedulerConfig
from ampa.scheduler_store import SchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    """In-memory store that avoids filesystem I/O."""

    def __init__(self):
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "dispatches": [],
        }

    def save(self):
        return None


def _make_config(**overrides):
    defaults = dict(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=10,
    )
    defaults.update(overrides)
    return SchedulerConfig(**defaults)


def _make_spec(
    command_id="delegation",
    command="echo delegation",
    command_type="delegation",
    max_runtime_minutes=None,
    title="Delegation Report",
    frequency_minutes=10,
    priority=0,
    metadata=None,
):
    return CommandSpec(
        command_id=command_id,
        command=command,
        requires_llm=False,
        frequency_minutes=frequency_minutes,
        priority=priority,
        metadata=metadata or {},
        title=title,
        max_runtime_minutes=max_runtime_minutes,
        command_type=command_type,
    )


def _make_work_item(
    work_id: str,
    stage: str = "delegated",
    status: str = "in_progress",
    title: str = "Test item",
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a work item dict resembling wl in_progress output."""
    if updated_at is None:
        updated_at = _utc_now().isoformat()
    return {
        "id": work_id,
        "title": title,
        "status": status,
        "stage": stage,
        "updatedAt": updated_at,
    }


def _stale_timestamp(age_seconds: int) -> str:
    """Return an ISO timestamp that is *age_seconds* in the past."""
    return (_utc_now() - dt.timedelta(seconds=age_seconds)).isoformat()


class ShellRecorder:
    """Records shell calls and returns configurable responses."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._responses: Dict[str, subprocess.CompletedProcess] = {}
        self._default = subprocess.CompletedProcess(
            args="", returncode=0, stdout="", stderr=""
        )

    def add_response(self, prefix: str, proc: subprocess.CompletedProcess):
        """Register a response for commands starting with *prefix*."""
        self._responses[prefix] = proc

    def __call__(self, *args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", "")
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        for prefix, proc in self._responses.items():
            if isinstance(cmd, str) and cmd.startswith(prefix):
                return proc
        return self._default


def _make_scheduler(
    shell: Optional[ShellRecorder] = None,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Scheduler:
    """Build a Scheduler with a DummyStore and injectable shell runner."""
    store = DummyStore()
    spec = _make_spec()
    store.add_command(spec)

    def noop_executor(_spec):
        start = _utc_now()
        return CommandRunResult(
            start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
        )

    scheduler = Scheduler(
        store,
        _make_config(),
        executor=noop_executor,
        run_shell=shell or ShellRecorder(),
    )
    return scheduler


# ---------------------------------------------------------------------------
# 1. Detection of stale delegated items
# ---------------------------------------------------------------------------


class TestStaleDelegationDetection:
    """Verify the watchdog finds items stuck in delegated stage."""

    def test_detects_stale_item_beyond_threshold(self):
        """An item updated 3 hours ago with 2-hour threshold is recovered."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-1",
            stage="delegated",
            title="Stuck task",
            updated_at=_stale_timestamp(10800),  # 3 hours ago
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert recovered[0]["work_item_id"] == "WL-1"
        assert recovered[0]["title"] == "Stuck task"
        assert recovered[0]["age_seconds"] >= 10800
        assert recovered[0]["reset_to"] == "open/plan_complete"

    def test_skips_item_below_threshold(self):
        """An item updated 30 minutes ago with 2-hour threshold is skipped."""
        shell = ShellRecorder()
        fresh_item = _make_work_item(
            "WL-2",
            stage="delegated",
            title="Recent task",
            updated_at=_stale_timestamp(1800),  # 30 minutes ago
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([fresh_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 0

    def test_skips_non_delegated_items(self):
        """Items in in_progress stage (not delegated) are ignored."""
        shell = ShellRecorder()
        in_progress_item = _make_work_item(
            "WL-3",
            stage="in_progress",
            title="Active work",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([in_progress_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 0

    def test_recovers_multiple_stale_items(self):
        """Multiple stale delegated items are all recovered."""
        shell = ShellRecorder()
        items = [
            _make_work_item(
                f"WL-{i}",
                stage="delegated",
                title=f"Task {i}",
                updated_at=_stale_timestamp(10800 + i * 100),
            )
            for i in range(3)
        ]
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps(items),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 3
        ids = {r["work_item_id"] for r in recovered}
        assert ids == {"WL-0", "WL-1", "WL-2"}

    def test_noop_when_no_delegated_items(self):
        """Returns empty list when no in-progress items exist."""
        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_noop_when_empty_response(self):
        """Returns empty list when wl in_progress returns null."""
        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout="null",
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_exact_threshold_boundary_not_recovered(self):
        """Item exactly at the threshold is NOT recovered (uses > not >=)."""
        shell = ShellRecorder()
        # Use a very precise age: exactly 7200 seconds (threshold)
        item = _make_work_item(
            "WL-BOUNDARY",
            stage="delegated",
            updated_at=_stale_timestamp(7200),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        # At exact boundary, age_s == threshold, condition is > so not recovered
        assert len(recovered) == 0

    def test_just_past_threshold_is_recovered(self):
        """Item 1 second past threshold IS recovered."""
        shell = ShellRecorder()
        item = _make_work_item(
            "WL-PAST",
            stage="delegated",
            updated_at=_stale_timestamp(7201),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="wl in_progress --json",
                returncode=0,
                stdout=json.dumps([item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert recovered[0]["work_item_id"] == "WL-PAST"


# ---------------------------------------------------------------------------
# 2. Recovery actions (wl update, wl comment)
# ---------------------------------------------------------------------------


class TestRecoveryActions:
    """Verify the watchdog issues correct wl update and wl comment commands."""

    def test_reset_command_uses_correct_state(self):
        """wl update resets to status=open stage=plan_complete."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-RESET",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        scheduler._delegation_orchestrator.recover_stale_delegations()

        # Find the wl update call
        update_calls = [
            c
            for c in shell.calls
            if isinstance(c["cmd"], str) and c["cmd"].startswith("wl update")
        ]
        assert len(update_calls) == 1
        cmd = update_calls[0]["cmd"]
        assert "WL-RESET" in cmd
        assert "--status open" in cmd
        assert "--stage plan_complete" in cmd
        assert "--json" in cmd

    def test_comment_posted_after_recovery(self):
        """A wl comment is posted documenting the watchdog action."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-COMMENT",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        scheduler._delegation_orchestrator.recover_stale_delegations()

        comment_calls = [
            c
            for c in shell.calls
            if isinstance(c["cmd"], str) and c["cmd"].startswith("wl comment add")
        ]
        assert len(comment_calls) == 1
        cmd = comment_calls[0]["cmd"]
        assert "WL-COMMENT" in cmd
        assert "ampa-watchdog" in cmd
        assert "Stale delegation recovery" in cmd

    def test_recovery_skipped_when_update_fails(self):
        """If wl update fails, the item is not counted as recovered."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-FAIL",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        # Make wl update fail
        shell.add_response(
            "wl update",
            subprocess.CompletedProcess(
                args="",
                returncode=1,
                stdout="",
                stderr="update failed",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 0

    def test_comment_failure_does_not_prevent_recovery(self):
        """If wl comment fails, the item is still counted as recovered."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-CFAIL",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        # Make wl comment fail by raising
        original_call = shell.__call__

        def failing_shell(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", "")
            if isinstance(cmd, str) and "wl comment add" in cmd:
                raise RuntimeError("comment service unavailable")
            return original_call(*args, **kwargs)

        shell.__call__ = failing_shell
        scheduler = _make_scheduler(shell=shell)
        # Re-assign the patched shell to both the scheduler and the
        # delegation orchestrator so recover_stale_delegations sees it.
        scheduler.run_shell = failing_shell
        scheduler._delegation_orchestrator.run_shell = failing_shell

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert recovered[0]["work_item_id"] == "WL-CFAIL"


# ---------------------------------------------------------------------------
# 3. Environment variable configuration
# ---------------------------------------------------------------------------


class TestThresholdConfiguration:
    """Verify AMPA_STALE_DELEGATION_THRESHOLD_SECONDS is respected."""

    def test_custom_threshold_from_env(self, monkeypatch):
        """A shorter threshold causes earlier recovery."""
        monkeypatch.setenv("AMPA_STALE_DELEGATION_THRESHOLD_SECONDS", "600")
        shell = ShellRecorder()
        # Item is 15 minutes old — would be skipped with default 2h but
        # should be recovered with 600s threshold
        item = _make_work_item(
            "WL-SHORT",
            stage="delegated",
            updated_at=_stale_timestamp(900),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1

    def test_invalid_threshold_falls_back_to_default(self, monkeypatch):
        """An invalid env value falls back to 7200s."""
        monkeypatch.setenv("AMPA_STALE_DELEGATION_THRESHOLD_SECONDS", "not-a-number")
        shell = ShellRecorder()
        # Item 1 hour old — below 7200s default
        item = _make_work_item(
            "WL-INVALID",
            stage="delegated",
            updated_at=_stale_timestamp(3600),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 0  # 3600 < 7200 default


# ---------------------------------------------------------------------------
# 4. Discord notification
# ---------------------------------------------------------------------------


class TestDiscordNotification:
    """Verify Discord notification is sent on recovery."""

    def test_discord_sent_on_recovery(self, monkeypatch):
        """A notification is sent when items are recovered."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-DISCORD",
            stage="delegated",
            title="Discord test",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        sent_notifications = []

        def mock_notify(title, body="", message_type="other", *, payload=None):
            sent_notifications.append(
                {"title": title, "body": body, "message_type": message_type}
            )
            return True

        with mock.patch.object(
            scheduler._delegation_orchestrator, "_notifications_module"
        ) as mock_notif:
            mock_notif.notify = mock_notify
            recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert len(sent_notifications) == 1
        assert sent_notifications[0]["message_type"] == "warning"
        assert "WL-DISCORD" in sent_notifications[0]["body"]

    def test_no_discord_when_nothing_recovered(self, monkeypatch):
        """No notification when no items are recovered."""
        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        with mock.patch.object(
            scheduler._delegation_orchestrator, "_notifications_module"
        ) as mock_notif:
            mock_notif.notify = mock.MagicMock()
            recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 0
        mock_notif.notify.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Error handling and resilience
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Verify the watchdog handles errors gracefully."""

    def test_wl_in_progress_failure_returns_empty(self):
        """When wl in_progress fails, returns empty list without crashing."""
        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=1,
                stdout="",
                stderr="wl not found",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_malformed_json_returns_empty(self):
        """When wl in_progress returns invalid JSON, returns empty list."""
        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout="not valid json{{{",
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_shell_exception_returns_empty(self):
        """When run_shell raises, returns empty list without crashing."""

        def exploding_shell(*args, **kwargs):
            raise OSError("no such process")

        scheduler = _make_scheduler()
        scheduler.run_shell = exploding_shell
        scheduler._delegation_orchestrator.run_shell = exploding_shell

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_item_without_id_skipped(self):
        """Items missing an 'id' field are silently skipped."""
        shell = ShellRecorder()
        bad_item = {
            "stage": "delegated",
            "updatedAt": _stale_timestamp(10800),
            # no id field
        }
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([bad_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_item_without_updated_at_skipped(self):
        """Items without an updatedAt field are skipped (age unknown)."""
        shell = ShellRecorder()
        no_ts_item = {
            "id": "WL-NOTS",
            "stage": "delegated",
            # no updatedAt
        }
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([no_ts_item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert recovered == []

    def test_nested_json_format_supported(self):
        """Handles wl output in nested {workItems: [...]} format."""
        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-NESTED",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps({"workItems": [stale_item]}),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert recovered[0]["work_item_id"] == "WL-NESTED"

    def test_alternative_updated_at_key(self):
        """Handles updated_at (snake_case) key variant."""
        shell = ShellRecorder()
        item = {
            "id": "WL-SNAKE",
            "stage": "delegated",
            "updated_at": _stale_timestamp(10800),  # snake_case variant
        }
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([item]),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        assert len(recovered) == 1
        assert recovered[0]["work_item_id"] == "WL-SNAKE"


# ---------------------------------------------------------------------------
# 6. Scheduled command integration
# ---------------------------------------------------------------------------


class TestScheduledCommandIntegration:
    """Verify the watchdog runs as its own scheduled command type."""

    def test_watchdog_runs_for_stale_delegation_watchdog_command_type(self):
        """start_command calls _recover_stale_delegations for the watchdog command type."""
        store = DummyStore()
        spec = _make_spec(
            command_id="stale-delegation-watchdog",
            command_type="stale-delegation-watchdog",
            command="echo watchdog",
            title="Stale Delegation Watchdog",
        )
        store.add_command(spec)

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([]),
                stderr="",
            ),
        )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=shell,
        )

        with mock.patch.object(
            scheduler._delegation_orchestrator,
            "recover_stale_delegations",
            wraps=scheduler._delegation_orchestrator.recover_stale_delegations,
        ) as mock_recover:
            scheduler.start_command(spec)
            mock_recover.assert_called_once()

    def test_watchdog_not_called_for_delegation_commands(self):
        """recover_stale_delegations is NOT called for delegation command type."""
        store = DummyStore()
        spec = _make_spec(command_type="delegation")
        store.add_command(spec)

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        shell = ShellRecorder()
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([]),
                stderr="",
            ),
        )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=shell,
        )

        with mock.patch.object(
            scheduler._delegation_orchestrator, "recover_stale_delegations"
        ) as mock_recover:
            scheduler.start_command(spec)
            mock_recover.assert_not_called()

    def test_watchdog_not_called_for_shell_commands(self):
        """recover_stale_delegations is NOT called for shell/heartbeat commands."""
        store = DummyStore()
        spec = _make_spec(
            command_id="heartbeat-1", command_type="shell", command="echo hi"
        )
        store.add_command(spec)

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=ShellRecorder(),
        )

        with mock.patch.object(
            scheduler._delegation_orchestrator, "recover_stale_delegations"
        ) as mock_recover:
            scheduler.start_command(spec)
            mock_recover.assert_not_called()

    def test_watchdog_failure_does_not_crash_start_command(self):
        """If the watchdog raises, start_command still returns a result."""
        store = DummyStore()
        spec = _make_spec(
            command_id="stale-delegation-watchdog",
            command_type="stale-delegation-watchdog",
            command="echo watchdog",
        )
        store.add_command(spec)

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=ShellRecorder(),
        )

        with mock.patch.object(
            scheduler._delegation_orchestrator,
            "recover_stale_delegations",
            side_effect=RuntimeError("watchdog exploded"),
        ):
            result = scheduler.start_command(spec)

        assert result is not None

    def test_recovered_items_appear_in_logs(self, caplog):
        """When items are recovered, a log message is emitted."""
        import logging

        store = DummyStore()
        spec = _make_spec(
            command_id="stale-delegation-watchdog",
            command_type="stale-delegation-watchdog",
            command="echo watchdog",
        )
        store.add_command(spec)

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        shell = ShellRecorder()
        stale_item = _make_work_item(
            "WL-LOG",
            stage="delegated",
            updated_at=_stale_timestamp(10800),
        )
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps([stale_item]),
                stderr="",
            ),
        )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=shell,
        )

        with caplog.at_level(logging.INFO, logger="ampa.scheduler"):
            scheduler.start_command(spec)

        watchdog_logs = [
            r for r in caplog.records if "watchdog recovered" in r.message.lower()
        ]
        assert len(watchdog_logs) >= 1


class TestWatchdogAutoRegistration:
    """Verify the watchdog command is auto-registered at scheduler init."""

    def test_watchdog_command_registered_on_init(self):
        """Scheduler.__init__ auto-registers the stale-delegation-watchdog command."""
        store = DummyStore()

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=ShellRecorder(),
        )

        commands = store.list_commands()
        watchdog_cmds = [
            c for c in commands if c.command_id == "stale-delegation-watchdog"
        ]
        assert len(watchdog_cmds) == 1
        wd = watchdog_cmds[0]
        assert wd.command_type == "stale-delegation-watchdog"
        assert wd.frequency_minutes == 30
        assert wd.requires_llm is False

    def test_watchdog_not_duplicated_on_reinit(self):
        """If the watchdog command already exists, init does not duplicate it."""
        store = DummyStore()

        def noop_executor(_spec):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )

        # Init twice
        Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=ShellRecorder(),
        )
        Scheduler(
            store,
            _make_config(),
            executor=noop_executor,
            run_shell=ShellRecorder(),
        )

        commands = store.list_commands()
        watchdog_cmds = [
            c for c in commands if c.command_id == "stale-delegation-watchdog"
        ]
        assert len(watchdog_cmds) == 1


# ---------------------------------------------------------------------------
# 7. Mixed scenarios
# ---------------------------------------------------------------------------


class TestMixedScenarios:
    """Verify correct behaviour with a mix of stale and fresh items."""

    def test_only_stale_delegated_items_recovered(self):
        """Mix of delegated (stale + fresh) and non-delegated items."""
        shell = ShellRecorder()
        items = [
            _make_work_item(
                "WL-STALE-1",
                stage="delegated",
                updated_at=_stale_timestamp(10800),
                title="Stale 1",
            ),
            _make_work_item(
                "WL-FRESH-1",
                stage="delegated",
                updated_at=_stale_timestamp(600),
                title="Fresh 1",
            ),
            _make_work_item(
                "WL-ACTIVE",
                stage="in_progress",
                updated_at=_stale_timestamp(10800),
                title="Active non-delegated",
            ),
            _make_work_item(
                "WL-STALE-2",
                stage="delegated",
                updated_at=_stale_timestamp(14400),
                title="Stale 2",
            ),
        ]
        shell.add_response(
            "wl in_progress",
            subprocess.CompletedProcess(
                args="",
                returncode=0,
                stdout=json.dumps(items),
                stderr="",
            ),
        )
        scheduler = _make_scheduler(shell=shell)

        recovered = scheduler._delegation_orchestrator.recover_stale_delegations()

        ids = {r["work_item_id"] for r in recovered}
        assert ids == {"WL-STALE-1", "WL-STALE-2"}
        assert len(recovered) == 2
