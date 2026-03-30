"""Integration tests: simulate hung opencode child processes and verify
scheduler timeout and recovery behaviour.

Work item: SA-0MLGGHJIL0STZDZZ
Parent: SA-0MLGE81WY17PH696 (delegation runner stuck)

These tests verify that:
1. The default_executor enforces timeouts on delegation/opencode commands and
   returns exit code 124 on timeout.
2. start_command() always clears the running flag even when the executor
   times out or raises.
3. _record_run() correctly records the failure (exit_code, running=False).
4. _clear_stale_running_states() recovers commands that were left marked
   running due to crashes.
5. The _run_shell_with_timeout wrapper converts TimeoutExpired to a
   CompletedProcess with returncode=124.
"""

import datetime as dt
import subprocess
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
from ampa.scheduler_executor import default_executor


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
    command="opencode run '/intake WL-1 do not ask questions'",
    command_type="delegation",
    max_runtime_minutes=None,
    title="Delegation",
    frequency_minutes=10,
    priority=0,
):
    return CommandSpec(
        command_id=command_id,
        command=command,
        requires_llm=False,
        frequency_minutes=frequency_minutes,
        priority=priority,
        metadata={},
        title=title,
        max_runtime_minutes=max_runtime_minutes,
        command_type=command_type,
    )


# ---------------------------------------------------------------------------
# 1. default_executor timeout enforcement
# ---------------------------------------------------------------------------


class TestDefaultExecutorTimeout:
    """Verify default_executor returns exit code 124 when subprocess times out."""

    def test_delegation_command_times_out(self, monkeypatch):
        """A delegation command that exceeds its timeout gets exit code 124."""
        spec = _make_spec(command_type="delegation")

        # Delegation commands now go through _run_command_with_graceful_timeout
        # (SIGTERM → SIGKILL) instead of subprocess.run directly.
        def mock_graceful_timeout(command, timeout, command_cwd):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        result = default_executor(spec)

        assert result.exit_code == 124
        assert isinstance(result, CommandRunResult)
        assert result.start_ts is not None
        assert result.end_ts is not None
        assert result.end_ts >= result.start_ts

    def test_opencode_run_command_times_out(self, monkeypatch):
        """A shell command containing 'opencode run' that hangs gets exit 124."""
        spec = _make_spec(
            command_id="intake-run",
            command="opencode run '/plan WL-42'",
            command_type="shell",
        )

        # Commands containing 'opencode run' also use the graceful timeout path.
        def mock_graceful_timeout(command, timeout, command_cwd):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        result = default_executor(spec)

        assert result.exit_code == 124

    def test_timeout_respects_max_runtime_minutes(self, monkeypatch):
        """max_runtime_minutes on spec is converted to seconds for subprocess."""
        spec = _make_spec(max_runtime_minutes=2)
        captured_timeout = {}

        def mock_graceful_timeout(command, timeout, command_cwd):
            captured_timeout["value"] = timeout
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        default_executor(spec)

        # 2 minutes = 120 seconds
        assert captured_timeout["value"] == 120

    def test_timeout_uses_delegation_env_override(self, monkeypatch):
        """AMPA_DELEGATION_OPENCODE_TIMEOUT overrides default timeout for delegation."""
        spec = _make_spec(command_type="delegation", max_runtime_minutes=None)
        captured_timeout = {}

        def mock_graceful_timeout(command, timeout, command_cwd):
            captured_timeout["value"] = timeout
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )
        monkeypatch.setenv("AMPA_DELEGATION_OPENCODE_TIMEOUT", "300")

        default_executor(spec)

        assert captured_timeout["value"] == 300

    def test_timeout_expired_captures_partial_output(self, monkeypatch):
        """Partial stdout/stderr from a timed-out process is preserved."""
        spec = _make_spec()

        def mock_graceful_timeout(command, timeout, command_cwd):
            exc = subprocess.TimeoutExpired(cmd=command, timeout=timeout)
            exc.output = "partial stdout before hang"
            exc.stderr = "partial stderr"
            raise exc

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        result = default_executor(spec)

        assert result.exit_code == 124
        assert "partial stdout before hang" in result.output
        assert "partial stderr" in result.output


# ---------------------------------------------------------------------------
# 2. start_command() clears running flag on timeout
# ---------------------------------------------------------------------------


class TestStartCommandRecovery:
    """Verify start_command always clears running and records failure."""

    def test_running_cleared_after_executor_timeout(self):
        """When executor returns exit 124 (timeout), running is cleared."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        def timeout_executor(_spec):
            start = _utc_now()
            end = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=end, exit_code=124, output="timed out"
            )

        scheduler = Scheduler(store, _make_config(), executor=timeout_executor)
        result = scheduler.start_command(spec)

        state = store.get_state(spec.command_id)
        assert state["running"] is False
        assert state["last_exit_code"] == 124
        assert result.exit_code == 124

    def test_running_cleared_after_executor_raises(self):
        """When executor raises an exception, running is still cleared."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        def crashing_executor(_spec):
            raise RuntimeError("opencode process segfaulted")

        scheduler = Scheduler(store, _make_config(), executor=crashing_executor)
        result = scheduler.start_command(spec)

        state = store.get_state(spec.command_id)
        assert state["running"] is False
        assert state["last_exit_code"] == 1  # generic failure
        assert result.exit_code == 1

    def test_running_cleared_after_keyboard_interrupt(self):
        """KeyboardInterrupt during execution still clears running."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        def interrupted_executor(_spec):
            raise KeyboardInterrupt()

        scheduler = Scheduler(store, _make_config(), executor=interrupted_executor)
        result = scheduler.start_command(spec)

        state = store.get_state(spec.command_id)
        assert state["running"] is False
        assert state["last_exit_code"] == 130  # SIGINT convention
        assert result.exit_code == 130

    def test_running_cleared_after_system_exit(self):
        """SystemExit during execution still clears running."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        def exit_executor(_spec):
            raise SystemExit(42)

        scheduler = Scheduler(store, _make_config(), executor=exit_executor)
        result = scheduler.start_command(spec)

        state = store.get_state(spec.command_id)
        assert state["running"] is False
        assert state["last_exit_code"] == 42
        assert result.exit_code == 42

    def test_failure_recorded_in_run_history(self):
        """A timeout failure appears in the command's run_history."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        def timeout_executor(_spec):
            start = _utc_now()
            end = _utc_now()
            return CommandRunResult(
                start_ts=start, end_ts=end, exit_code=124, output="timeout"
            )

        scheduler = Scheduler(store, _make_config(), executor=timeout_executor)
        scheduler.start_command(spec)

        state = store.get_state(spec.command_id)
        history = state.get("run_history", [])
        assert len(history) == 1
        assert history[0]["exit_code"] == 124

    def test_scheduler_can_run_again_after_timeout(self):
        """After a timeout, the same command can be scheduled and run again."""
        store = DummyStore()
        spec = _make_spec(command_type="shell")
        store.add_command(spec)

        call_count = 0

        def counting_executor(_spec):
            nonlocal call_count
            call_count += 1
            start = _utc_now()
            end = _utc_now()
            if call_count == 1:
                return CommandRunResult(
                    start_ts=start, end_ts=end, exit_code=124, output="timeout"
                )
            return CommandRunResult(
                start_ts=start, end_ts=end, exit_code=0, output="success"
            )

        scheduler = Scheduler(store, _make_config(), executor=counting_executor)

        # First run: times out
        result1 = scheduler.start_command(spec)
        assert result1.exit_code == 124
        state1 = store.get_state(spec.command_id)
        assert state1["running"] is False

        # Second run: succeeds
        result2 = scheduler.start_command(spec)
        assert result2.exit_code == 0
        state2 = store.get_state(spec.command_id)
        assert state2["running"] is False
        assert state2["last_exit_code"] == 0

        history = state2.get("run_history", [])
        assert len(history) == 2
        assert history[0]["exit_code"] == 124
        assert history[1]["exit_code"] == 0


# ---------------------------------------------------------------------------
# 3. _clear_stale_running_states recovery
# ---------------------------------------------------------------------------


class TestStaleRunningRecovery:
    """Verify _clear_stale_running_states clears old stuck commands."""

    def test_stale_running_cleared_on_init(self, monkeypatch):
        """Commands stuck running beyond threshold are cleared at init."""
        monkeypatch.setenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "60")
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        store = DummyStore()
        spec = _make_spec()
        store.add_command(spec)
        # Simulate a command that has been running for 120 seconds (> 60s threshold)
        two_minutes_ago = _utc_now() - dt.timedelta(seconds=120)
        store.update_state(
            spec.command_id,
            {
                "running": True,
                "last_start_ts": _to_iso(two_minutes_ago),
            },
        )

        # Creating a Scheduler triggers _clear_stale_running_states
        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
        )

        state = store.get_state(spec.command_id)
        assert state["running"] is False

    def test_recent_running_not_cleared(self, monkeypatch):
        """Commands running within threshold are NOT cleared."""
        monkeypatch.setenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "300")
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        store = DummyStore()
        spec = _make_spec()
        store.add_command(spec)
        # Simulate a command that started 10 seconds ago (< 300s threshold)
        ten_secs_ago = _utc_now() - dt.timedelta(seconds=10)
        store.update_state(
            spec.command_id,
            {
                "running": True,
                "last_start_ts": _to_iso(ten_secs_ago),
            },
        )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
        )

        state = store.get_state(spec.command_id)
        assert state["running"] is True

    def test_stale_with_no_start_ts_cleared(self, monkeypatch):
        """Running command with no last_start_ts is treated as stale."""
        monkeypatch.setenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "60")
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        store = DummyStore()
        spec = _make_spec()
        store.add_command(spec)
        store.update_state(
            spec.command_id,
            {
                "running": True,
                # no last_start_ts — age is None, treated as stale
            },
        )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
        )

        state = store.get_state(spec.command_id)
        assert state["running"] is False

    def test_multiple_stale_commands_cleared(self, monkeypatch):
        """Multiple stuck commands are all cleared on init."""
        monkeypatch.setenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "60")
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        store = DummyStore()
        old_time = _utc_now() - dt.timedelta(seconds=120)

        for i in range(3):
            spec = _make_spec(command_id=f"cmd-{i}", command_type="shell")
            store.add_command(spec)
            store.update_state(
                spec.command_id,
                {"running": True, "last_start_ts": _to_iso(old_time)},
            )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
        )

        for i in range(3):
            state = store.get_state(f"cmd-{i}")
            assert state["running"] is False, f"cmd-{i} should have been cleared"


# ---------------------------------------------------------------------------
# 4. _run_shell_with_timeout wrapper
# ---------------------------------------------------------------------------


class TestRunShellWithTimeout:
    """Verify the injected run_shell wrapper handles TimeoutExpired."""

    def test_timeout_expired_converted_to_exit_124(self, monkeypatch):
        """When run_shell raises TimeoutExpired, it returns exit code 124."""
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "5")

        def hanging_shell(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        store = DummyStore()
        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
            run_shell=hanging_shell,
        )

        result = scheduler.run_shell("sleep 999", shell=True, capture_output=True)

        assert result.returncode == 124

    def test_timeout_injected_when_not_provided(self, monkeypatch):
        """When caller does not pass timeout, the wrapper injects default."""
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "42")

        captured = {}

        def spy_shell(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(args=args[0], returncode=0)

        store = DummyStore()
        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
            run_shell=spy_shell,
        )

        scheduler.run_shell("echo hello", shell=True)
        assert captured["timeout"] == 42

    def test_explicit_timeout_respected(self, monkeypatch):
        """When caller passes explicit timeout, wrapper does not override it."""
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        captured = {}

        def spy_shell(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(args=args[0], returncode=0)

        store = DummyStore()
        scheduler = Scheduler(
            store,
            _make_config(),
            executor=lambda _: None,
            run_shell=spy_shell,
        )

        scheduler.run_shell("echo hello", shell=True, timeout=10)
        assert captured["timeout"] == 10


# ---------------------------------------------------------------------------
# 5. End-to-end: simulated hung opencode child
# ---------------------------------------------------------------------------


class TestEndToEndHungChild:
    """Full lifecycle: delegation command hangs -> timeout -> recovery."""

    def test_hung_delegation_full_lifecycle(self, monkeypatch):
        """Simulate a command whose opencode run call hangs.

        Uses command_type="shell" to exercise the non-delegation code path
        in start_command (the delegation path invokes _inspect_idle_delegation
        and _run_idle_delegation which require live `wl` commands).  The
        timeout/recovery behaviour under test (running flag, state recording,
        run_history) is identical across all command types.

        Verifies:
        - running flag is set before execution
        - executor times out and returns exit 124
        - running flag is cleared after timeout
        - failure is recorded in state and run_history
        - command can run successfully on retry
        """
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "3600")

        store = DummyStore()
        spec = _make_spec(
            command_id="hung-child",
            command="opencode run '/implement WL-99'",
            command_type="shell",
        )
        store.add_command(spec)

        execution_log = []

        def hung_then_ok_executor(_spec):
            start = _utc_now()
            call_num = len(execution_log) + 1
            execution_log.append(call_num)

            if call_num == 1:
                # First call: simulate timeout
                end = _utc_now()
                return CommandRunResult(
                    start_ts=start,
                    end_ts=end,
                    exit_code=124,
                    output="Command delegation timed out after 3600 seconds",
                )
            else:
                # Second call: simulate success
                end = _utc_now()
                return CommandRunResult(
                    start_ts=start,
                    end_ts=end,
                    exit_code=0,
                    output="Delegation completed successfully",
                )

        scheduler = Scheduler(
            store,
            _make_config(),
            executor=hung_then_ok_executor,
        )

        # Pre-condition: not running
        state_before = store.get_state(spec.command_id)
        assert state_before.get("running") is not True

        # Run 1: hangs/times out
        result1 = scheduler.start_command(spec)
        assert result1.exit_code == 124

        state_after_timeout = store.get_state(spec.command_id)
        assert state_after_timeout["running"] is False
        assert state_after_timeout["last_exit_code"] == 124

        # Run 2: succeeds
        result2 = scheduler.start_command(spec)
        assert result2.exit_code == 0

        state_after_success = store.get_state(spec.command_id)
        assert state_after_success["running"] is False
        assert state_after_success["last_exit_code"] == 0

        # Both runs recorded in history
        history = state_after_success.get("run_history", [])
        assert len(history) == 2
        assert history[0]["exit_code"] == 124
        assert history[1]["exit_code"] == 0

    def test_real_subprocess_timeout(self, monkeypatch):
        """Use default_executor with a real subprocess that exceeds timeout.

        This test spawns 'sleep 60' with a 1-second timeout to verify the
        full subprocess.run -> TimeoutExpired -> exit 124 path without mocks
        on the subprocess layer itself.
        """

        spec = _make_spec(
            command_id="slow-cmd",
            command="sleep 60",
            command_type="delegation",
            max_runtime_minutes=None,
        )

        # Set a very short timeout so the test completes quickly
        monkeypatch.setenv("AMPA_DELEGATION_OPENCODE_TIMEOUT", "1")
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "1")

        result = default_executor(spec)

        assert result.exit_code == 124
        assert isinstance(result, CommandRunResult)
        # Duration should be approximately 1 second (the timeout), not 60
        assert result.duration_seconds < 5

    def test_real_subprocess_timeout_through_scheduler(self, monkeypatch):
        """End-to-end through Scheduler: real subprocess timeout clears state.

        Injects default_executor with a mocked _run_command_with_graceful_timeout
        that raises TimeoutExpired to verify the full path:
        Scheduler.start_command -> default_executor -> TimeoutExpired ->
        exit 124 -> state cleared.

        This avoids actually sleeping for 60+ seconds while still exercising
        the real default_executor (not a test double).
        """
        monkeypatch.setenv("AMPA_CMD_TIMEOUT_SECONDS", "5")

        store = DummyStore()
        spec = _make_spec(
            command_id="real-timeout",
            command="opencode run '/implement WL-99'",
            command_type="shell",
        )
        store.add_command(spec)

        # 'opencode run' in command → uses _run_command_with_graceful_timeout
        def mock_graceful_timeout(command, timeout, command_cwd):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        # Use default_executor (the real one, not a mock)
        scheduler = Scheduler(store, _make_config())

        result = scheduler.start_command(spec)

        assert result.exit_code == 124
        state = store.get_state(spec.command_id)
        assert state["running"] is False
        assert state["last_exit_code"] == 124

        history = state.get("run_history", [])
        assert len(history) == 1
        assert history[0]["exit_code"] == 124


# ---------------------------------------------------------------------------
# 6. Discord notification on timeout
# ---------------------------------------------------------------------------


class TestTimeoutDiscordNotification:
    """Verify Discord notification is sent when a command times out."""

    def test_default_executor_sends_discord_on_timeout(self, monkeypatch):
        """When a command times out, a notification is sent via notifications.notify()."""
        spec = _make_spec()
        notify_calls = []

        # Delegation commands use _run_command_with_graceful_timeout now.
        def mock_graceful_timeout(command, timeout, command_cwd):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        def mock_notify(title="", body="", message_type="other", **kwargs):
            notify_calls.append(
                {"title": title, "body": body, "message_type": message_type}
            )
            return True

        monkeypatch.setattr(
            "ampa.scheduler_executor._run_command_with_graceful_timeout",
            mock_graceful_timeout,
        )

        # Mock the notifications module
        monkeypatch.setattr("ampa.scheduler.notifications_module.notify", mock_notify)

        result = default_executor(spec)

        assert result.exit_code == 124
        assert len(notify_calls) == 1
        assert notify_calls[0]["message_type"] == "error"
