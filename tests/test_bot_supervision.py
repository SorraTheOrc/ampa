"""Tests for Discord bot process supervision in the Scheduler.

Verifies that the scheduler starts, monitors, and restarts the Discord bot
process via ``BotSupervisor.ensure_running()``, and cleanly shuts it down via
``BotSupervisor.shutdown()``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest import mock

import pytest

from ampa.scheduler_types import SchedulerConfig
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    def __init__(self):
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self):
        return None


def _make_scheduler(**overrides) -> Scheduler:
    """Create a Scheduler with minimal configuration for supervision tests."""
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=1,
        global_min_interval_seconds=1,
        priority_weight=1.0,
        store_path=":memory:",
        llm_healthcheck_url="",
        max_run_history=10,
    )
    return Scheduler(
        store=store,
        config=config,
        executor=lambda spec: None,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Tests: BotSupervisor.ensure_running
# ---------------------------------------------------------------------------


class TestEnsureBotRunning:
    """Tests for BotSupervisor.ensure_running() method."""

    def test_noop_when_no_token(self, monkeypatch):
        """If AMPA_DISCORD_BOT_TOKEN is not set, ensure_running is a no-op."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        sched = _make_scheduler()
        sched._bot_supervisor.ensure_running()
        assert sched._bot_supervisor._bot_process is None

    def test_starts_bot_when_token_set(self, monkeypatch):
        """Bot process is spawned when token is configured."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()

        fake_proc = mock.MagicMock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None  # still alive

        with mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            sched._bot_supervisor.ensure_running()

        mock_popen.assert_called_once_with(
            [sys.executable, "-m", "ampa.discord_bot"],
            start_new_session=True,
        )
        assert sched._bot_supervisor._bot_process is fake_proc
        assert sched._bot_supervisor._bot_consecutive_failures == 0

    def test_noop_when_bot_already_alive(self, monkeypatch):
        """If bot process is still running, ensure_running does nothing."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()

        fake_proc = mock.MagicMock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None  # alive
        sched._bot_supervisor._bot_process = fake_proc

        with mock.patch("subprocess.Popen") as mock_popen:
            sched._bot_supervisor.ensure_running()

        mock_popen.assert_not_called()
        assert sched._bot_supervisor._bot_process is fake_proc

    def test_restarts_when_bot_exited(self, monkeypatch):
        """If bot process has exited, it is restarted."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()

        dead_proc = mock.MagicMock()
        dead_proc.pid = 11111
        dead_proc.poll.return_value = 1  # exited with code 1
        sched._bot_supervisor._bot_process = dead_proc

        new_proc = mock.MagicMock()
        new_proc.pid = 22222
        new_proc.poll.return_value = None

        with mock.patch("subprocess.Popen", return_value=new_proc):
            sched._bot_supervisor.ensure_running()

        assert sched._bot_supervisor._bot_process is new_proc
        assert sched._bot_supervisor._bot_consecutive_failures == 0

    def test_increments_failure_counter_on_start_error(self, monkeypatch):
        """If Popen raises, the consecutive failure counter increments."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()

        with mock.patch("subprocess.Popen", side_effect=OSError("cannot start")):
            sched._bot_supervisor.ensure_running()

        assert sched._bot_supervisor._bot_process is None
        assert sched._bot_supervisor._bot_consecutive_failures == 1

    def test_stops_retrying_after_max_failures(self, monkeypatch):
        """After 3 consecutive failures, ensure_running stops trying."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()

        with mock.patch("subprocess.Popen", side_effect=OSError("cannot start")):
            for _ in range(3):
                sched._bot_supervisor.ensure_running()

        assert sched._bot_supervisor._bot_consecutive_failures == 3

        # 4th call should be a no-op — no Popen attempt.
        with mock.patch("subprocess.Popen") as mock_popen:
            sched._bot_supervisor.ensure_running()
        mock_popen.assert_not_called()

    def test_failure_counter_resets_on_success(self, monkeypatch):
        """A successful start resets the consecutive failure counter."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()
        sched._bot_supervisor._bot_consecutive_failures = 2  # two prior failures

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99999
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            sched._bot_supervisor.ensure_running()

        assert sched._bot_supervisor._bot_consecutive_failures == 0
        assert sched._bot_supervisor._bot_process is fake_proc


# ---------------------------------------------------------------------------
# Tests: BotSupervisor.shutdown
# ---------------------------------------------------------------------------


class TestShutdownBot:
    """Tests for BotSupervisor.shutdown() method."""

    def test_noop_when_no_process(self):
        """If no bot process exists, shutdown is a no-op."""
        sched = _make_scheduler()
        sched._bot_supervisor.shutdown()  # should not raise
        assert sched._bot_supervisor._bot_process is None

    def test_noop_when_already_exited(self):
        """If the bot has already exited, just cleans up the reference."""
        sched = _make_scheduler()
        dead_proc = mock.MagicMock()
        dead_proc.poll.return_value = 0
        sched._bot_supervisor._bot_process = dead_proc

        sched._bot_supervisor.shutdown()

        dead_proc.terminate.assert_not_called()
        assert sched._bot_supervisor._bot_process is None

    def test_sends_sigterm_and_waits(self):
        """Bot process receives SIGTERM and exits cleanly."""
        sched = _make_scheduler()
        proc = mock.MagicMock()
        proc.pid = 54321
        proc.poll.return_value = None  # alive
        proc.wait.return_value = 0
        sched._bot_supervisor._bot_process = proc

        sched._bot_supervisor.shutdown()

        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        proc.kill.assert_not_called()
        assert sched._bot_supervisor._bot_process is None

    def test_sends_sigkill_on_timeout(self):
        """If SIGTERM doesn't work within timeout, SIGKILL is sent."""
        sched = _make_scheduler()
        proc = mock.MagicMock()
        proc.pid = 54321
        proc.poll.return_value = None
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("cmd", 5),  # first wait times out
            None,  # second wait (after kill) succeeds
        ]
        sched._bot_supervisor._bot_process = proc

        sched._bot_supervisor.shutdown()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert sched._bot_supervisor._bot_process is None

    def test_handles_exception_during_shutdown(self):
        """Exceptions during shutdown are caught, not propagated."""
        sched = _make_scheduler()
        proc = mock.MagicMock()
        proc.pid = 54321
        proc.poll.return_value = None
        proc.terminate.side_effect = OSError("cannot terminate")
        sched._bot_supervisor._bot_process = proc

        sched._bot_supervisor.shutdown()  # should not raise
        assert sched._bot_supervisor._bot_process is None


# ---------------------------------------------------------------------------
# Tests: run_forever integration with bot supervision
# ---------------------------------------------------------------------------


class TestRunForeverBotSupervision:
    """Tests that run_forever integrates bot supervision correctly."""

    def test_run_forever_calls_ensure_bot_running(self, monkeypatch):
        """run_forever calls BotSupervisor.ensure_running on startup and each cycle."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        sched = _make_scheduler()

        call_count = 0
        cycle_count = 0

        orig_ensure = sched._bot_supervisor.ensure_running

        def counting_ensure():
            nonlocal call_count
            call_count += 1
            orig_ensure()

        sched._bot_supervisor.ensure_running = counting_ensure

        orig_run_once = sched.run_once

        def counting_run_once():
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count >= 2:
                raise KeyboardInterrupt("stop after 2 cycles")
            return orig_run_once()

        sched.run_once = counting_run_once
        sched._post_startup_message = mock.MagicMock()

        # Patch sleep to be instant.
        monkeypatch.setattr(time, "sleep", lambda _: None)

        try:
            sched.run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass

        # Called once at startup + once per cycle (at least 2 cycles).
        assert call_count >= 3  # 1 startup + 2 cycles

    def test_run_forever_shuts_down_bot_on_exit(self, monkeypatch):
        """run_forever calls BotSupervisor.shutdown in the finally block."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        sched = _make_scheduler()
        sched._post_startup_message = mock.MagicMock()

        shutdown_called = False
        orig_shutdown = sched._bot_supervisor.shutdown

        def tracking_shutdown():
            nonlocal shutdown_called
            shutdown_called = True
            orig_shutdown()

        sched._bot_supervisor.shutdown = tracking_shutdown

        def raising_run_once():
            raise KeyboardInterrupt("stop immediately")

        sched.run_once = raising_run_once
        monkeypatch.setattr(time, "sleep", lambda _: None)

        try:
            sched.run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass

        assert shutdown_called is True

    def test_run_forever_signal_handler_installed(self, monkeypatch):
        """run_forever installs signal handlers for SIGTERM and SIGINT."""
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        sched = _make_scheduler()
        sched._post_startup_message = mock.MagicMock()
        sched._bot_supervisor.ensure_running = mock.MagicMock()

        installed_signals = {}

        def fake_signal(signum, handler):
            installed_signals[signum] = handler

        monkeypatch.setattr(signal, "signal", fake_signal)

        def raising_run_once():
            raise KeyboardInterrupt("stop immediately")

        sched.run_once = raising_run_once
        monkeypatch.setattr(time, "sleep", lambda _: None)

        try:
            sched.run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass

        assert signal.SIGTERM in installed_signals
        assert signal.SIGINT in installed_signals
