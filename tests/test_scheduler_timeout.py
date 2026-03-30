"""Tests for the scheduler's delegation watchdog timeout (DELEGATION_TIMEOUT_SECONDS).

These tests verify that:
1. DELEGATION_TIMEOUT_SECONDS env var is respected by the executor.
2. AMPA_DELEGATION_OPENCODE_TIMEOUT is still honoured (backward compatibility).
3. The SIGTERM → SIGKILL escalation path is exercised when a delegation
   command hangs past the timeout.
4. The scheduler clears the running flag (running=False) after a timeout.
5. The run history records exit_code=124 and a timeout note for delegation
   timeouts.
6. Non-delegation commands are not subject to the SIGTERM/SIGKILL path.
"""

from __future__ import annotations

import signal
import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    RunResult,
    SchedulerConfig,
    _utc_now,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_executor import _run_command_with_graceful_timeout


# ---------------------------------------------------------------------------
# Shared helpers (mirrors the pattern in test_stale_delegation_watchdog.py)
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
    command_id: str = "delegation",
    command: str = "opencode run --task test",
    command_type: str = "delegation",
    max_runtime_minutes: Optional[float] = None,
    title: str = "Delegation Test",
    frequency_minutes: float = 10,
    priority: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> CommandSpec:
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


def _make_scheduler(executor=None) -> Scheduler:
    """Build a Scheduler backed by an in-memory store."""
    store = DummyStore()
    spec = _make_spec()
    store.add_command(spec)
    if executor is None:
        def _noop_executor(s):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=_utc_now(), exit_code=0, output=""
            )
        executor = _noop_executor
    return Scheduler(
        store,
        _make_config(),
        executor=executor,
        run_shell=mock.MagicMock(
            return_value=subprocess.CompletedProcess(
                args="", returncode=0, stdout="", stderr=""
            )
        ),
    )


# ---------------------------------------------------------------------------
# 1. DELEGATION_TIMEOUT_SECONDS env var is used by the executor
# ---------------------------------------------------------------------------


class TestDelegationTimeoutEnvVar:
    """Verify timeout env-var resolution in default_executor."""

    def test_delegation_timeout_seconds_used(self, monkeypatch):
        """DELEGATION_TIMEOUT_SECONDS configures the delegation timeout."""
        monkeypatch.setenv("DELEGATION_TIMEOUT_SECONDS", "42")
        monkeypatch.delenv("AMPA_DELEGATION_OPENCODE_TIMEOUT", raising=False)
        monkeypatch.delenv("AMPA_CMD_TIMEOUT_SECONDS", raising=False)

        spec = _make_spec(command="opencode run --task hi", command_type="delegation")

        calls: List[Any] = []

        def fake_graceful(command, timeout, cwd):
            calls.append({"command": command, "timeout": timeout})
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="ok", stderr=""
            )

        with mock.patch(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            side_effect=fake_graceful,
        ):
            from ampa.scheduler_executor import default_executor
            default_executor(spec, command_cwd=None)

        assert calls, "graceful timeout helper should have been called"
        assert calls[0]["timeout"] == 42

    def test_legacy_env_var_still_works(self, monkeypatch):
        """AMPA_DELEGATION_OPENCODE_TIMEOUT is honoured when the new var is absent."""
        monkeypatch.delenv("DELEGATION_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setenv("AMPA_DELEGATION_OPENCODE_TIMEOUT", "99")
        monkeypatch.delenv("AMPA_CMD_TIMEOUT_SECONDS", raising=False)

        spec = _make_spec(command="opencode run --task hi", command_type="delegation")
        calls: List[Any] = []

        def fake_graceful(command, timeout, cwd):
            calls.append({"timeout": timeout})
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="ok", stderr=""
            )

        with mock.patch(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            side_effect=fake_graceful,
        ):
            from ampa.scheduler_executor import default_executor
            default_executor(spec, command_cwd=None)

        assert calls
        assert calls[0]["timeout"] == 99

    def test_new_var_takes_precedence_over_legacy(self, monkeypatch):
        """DELEGATION_TIMEOUT_SECONDS wins when both vars are set."""
        monkeypatch.setenv("DELEGATION_TIMEOUT_SECONDS", "123")
        monkeypatch.setenv("AMPA_DELEGATION_OPENCODE_TIMEOUT", "456")
        monkeypatch.delenv("AMPA_CMD_TIMEOUT_SECONDS", raising=False)

        spec = _make_spec(command="opencode run --task hi", command_type="delegation")
        calls: List[Any] = []

        def fake_graceful(command, timeout, cwd):
            calls.append({"timeout": timeout})
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="ok", stderr=""
            )

        with mock.patch(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            side_effect=fake_graceful,
        ):
            from ampa.scheduler_executor import default_executor
            default_executor(spec, command_cwd=None)

        assert calls
        assert calls[0]["timeout"] == 123


# ---------------------------------------------------------------------------
# 2. SIGTERM → SIGKILL escalation in _run_command_with_graceful_timeout
# ---------------------------------------------------------------------------


class TestGracefulTimeoutHelper:
    """Unit-test the _run_command_with_graceful_timeout helper directly."""

    def test_sigterm_sent_on_timeout(self):
        """SIGTERM is sent to the process group when the primary timeout fires."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        # First communicate() raises TimeoutExpired (primary timeout)
        # Second communicate() returns normally (process exited after SIGTERM)
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="cmd", timeout=1),
            ("stdout_partial", ""),
        ]

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with mock.patch("ampa.scheduler_executor.os.killpg") as mock_killpg:
                with pytest.raises(subprocess.TimeoutExpired):
                    _run_command_with_graceful_timeout(
                        "sleep 100", timeout=1, command_cwd=None
                    )

        mock_killpg.assert_any_call(12345, signal.SIGTERM)
        # SIGKILL should NOT have been sent (process died after SIGTERM)
        sigkill_calls = [
            c for c in mock_killpg.call_args_list if c.args[1] == signal.SIGKILL
        ]
        assert not sigkill_calls

    def test_sigkill_sent_when_sigterm_insufficient(self):
        """SIGKILL is sent to the process group if it does not exit after SIGTERM."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        # Primary timeout fires, then grace-period timeout also fires → SIGKILL
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="cmd", timeout=1),
            subprocess.TimeoutExpired(cmd="cmd", timeout=5),  # grace period also times out
            ("", ""),  # final communicate after kill
        ]

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with mock.patch("ampa.scheduler_executor.os.killpg") as mock_killpg:
                with pytest.raises(subprocess.TimeoutExpired):
                    _run_command_with_graceful_timeout(
                        "sleep 100", timeout=1, command_cwd=None
                    )

        sigterm_calls = [
            c for c in mock_killpg.call_args_list if c.args[1] == signal.SIGTERM
        ]
        sigkill_calls = [
            c for c in mock_killpg.call_args_list if c.args[1] == signal.SIGKILL
        ]
        assert sigterm_calls, "SIGTERM should have been sent"
        assert sigkill_calls, "SIGKILL should have been sent after grace period"

    def test_normal_completion_returns_result(self):
        """A process that finishes within the timeout returns a CompletedProcess."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_proc.communicate.return_value = ("hello", "")
        mock_proc.returncode = 0

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            result = _run_command_with_graceful_timeout(
                "echo hello", timeout=10, command_cwd=None
            )

        assert result.returncode == 0
        assert result.stdout == "hello"


# ---------------------------------------------------------------------------
# 3. Running flag is cleared after delegation timeout (end-to-end)
# ---------------------------------------------------------------------------


class TestRunningFlagClearedOnTimeout:
    """Ensure running=False after a delegation command times out."""

    def test_running_flag_cleared_after_timeout(self):
        """After a timeout (exit_code=124), running must be False in the store."""
        spec = _make_spec()

        # Executor simulates a timed-out delegation (exit_code=124)
        def timeout_executor(s):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start,
                end_ts=_utc_now(),
                exit_code=124,
                output="[timeout] delegation timed out",
            )

        sched = _make_scheduler(executor=timeout_executor)
        # Add our spec to the store
        sched.store.add_command(spec)

        with mock.patch.object(
            sched._delegation_orchestrator,
            "execute",
            return_value=CommandRunResult(
                start_ts=_utc_now(),
                end_ts=_utc_now(),
                exit_code=124,
                output="[timeout]",
            ),
        ):
            sched.start_command(spec)

        state = sched.store.get_state(spec.command_id)
        assert state.get("running") is False, (
            "running flag must be False after delegation timeout"
        )
        assert state.get("last_exit_code") == 124

    def test_running_flag_cleared_after_timeout_non_delegation(self):
        """Non-delegation commands: running=False after timeout exit_code=124."""
        spec = _make_spec(
            command_id="simple-cmd",
            command="echo hi",
            command_type="shell",
        )

        def timeout_executor(s):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start,
                end_ts=_utc_now(),
                exit_code=124,
                output="",
            )

        sched = _make_scheduler(executor=timeout_executor)
        sched.store.add_command(spec)
        sched.start_command(spec)

        state = sched.store.get_state(spec.command_id)
        assert state.get("running") is False
        assert state.get("last_exit_code") == 124


# ---------------------------------------------------------------------------
# 4. Timeout note is recorded in run history for delegation commands
# ---------------------------------------------------------------------------


class TestTimeoutNoteInRunHistory:
    """Timeout events for delegation commands persist a note in run history."""

    def test_timeout_note_in_output(self):
        """exit_code=124 for delegation → timeout note in last_output."""
        spec = _make_spec()

        def timeout_executor(s):
            start = _utc_now()
            return CommandRunResult(
                start_ts=start,
                end_ts=_utc_now(),
                exit_code=124,
                output="",
            )

        sched = _make_scheduler(executor=timeout_executor)
        sched.store.add_command(spec)

        with mock.patch.object(
            sched._delegation_orchestrator,
            "execute",
            side_effect=lambda spec, run, out: CommandRunResult(
                start_ts=run.start_ts,
                end_ts=run.end_ts,
                exit_code=run.exit_code,
                output=out or "",
            ),
        ):
            sched.start_command(spec)

        state = sched.store.get_state(spec.command_id)
        last_output = state.get("last_output") or ""
        assert "timeout" in last_output.lower(), (
            f"Expected 'timeout' in last_output, got: {last_output!r}"
        )
