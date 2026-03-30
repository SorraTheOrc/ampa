"""Tests for retry / exponential-backoff logic in ampa.notifications.

These tests verify that:
- On socket failure the sender retries with exponential backoff up to the
  configured maximum attempts.
- Each retry is logged with its attempt number and the error encountered.
- Final failure after exhausting retries is logged at ERROR level.
- On success after transient failures, no ERROR is logged.
- The AMPA_MAX_RETRIES and AMPA_BACKOFF_BASE_SECONDS env vars are respected.
- Backoff delays follow the expected exponential pattern (base * 2^(n-1)).

All tests mock ``time.sleep`` so they execute instantly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from ampa.notifications import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    _retry_config,
    _send_via_socket,
    notify,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSocketServer:
    """A minimal Unix socket server that controls success/failure per request."""

    def __init__(
        self,
        socket_path: str,
        *,
        responses: List[Dict[str, Any]] | None = None,
        ok: bool = True,
        error: str = "",
    ):
        self.socket_path = socket_path
        # If responses is provided, pop from front for each request.
        # Otherwise use the static ok/error values.
        self._responses = list(responses) if responses else None
        self._ok = ok
        self._error = error
        self.received: List[Dict[str, Any]] = []
        self._server = None

    async def _handle(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                self.received.append(data)
            except Exception:
                pass

            if self._responses:
                resp = self._responses.pop(0)
            else:
                resp = {"ok": self._ok}
                if self._error:
                    resp["error"] = self._error

            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path
        )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


def _run_sync_in_async(sync_fn, *args, **kwargs):
    """Run a synchronous function in a background thread."""

    async def _inner():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: sync_fn(*args, **kwargs))

    return _inner()


# ---------------------------------------------------------------------------
# Tests: _retry_config
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("AMPA_MAX_RETRIES", raising=False)
        monkeypatch.delenv("AMPA_BACKOFF_BASE_SECONDS", raising=False)
        max_r, backoff = _retry_config()
        assert max_r == DEFAULT_MAX_RETRIES
        assert backoff == DEFAULT_BACKOFF_BASE_SECONDS

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AMPA_MAX_RETRIES", "5")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "3.5")
        max_r, backoff = _retry_config()
        assert max_r == 5
        assert backoff == 3.5

    def test_min_retries_clamped_to_1(self, monkeypatch):
        monkeypatch.setenv("AMPA_MAX_RETRIES", "0")
        max_r, _ = _retry_config()
        assert max_r == 1

    def test_negative_retries_clamped_to_1(self, monkeypatch):
        monkeypatch.setenv("AMPA_MAX_RETRIES", "-5")
        max_r, _ = _retry_config()
        assert max_r == 1

    def test_invalid_retries_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AMPA_MAX_RETRIES", "not-a-number")
        max_r, _ = _retry_config()
        assert max_r == DEFAULT_MAX_RETRIES

    def test_zero_backoff_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "0")
        _, backoff = _retry_config()
        assert backoff == DEFAULT_BACKOFF_BASE_SECONDS

    def test_negative_backoff_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "-1")
        _, backoff = _retry_config()
        assert backoff == DEFAULT_BACKOFF_BASE_SECONDS

    def test_invalid_backoff_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "abc")
        _, backoff = _retry_config()
        assert backoff == DEFAULT_BACKOFF_BASE_SECONDS


# ---------------------------------------------------------------------------
# Tests: retry with socket failures then success
# ---------------------------------------------------------------------------


class TestRetryThenSuccess:
    """Simulate transient socket failures followed by success."""

    @patch("ampa.notifications.time.sleep")
    def test_succeeds_after_transient_failures(self, mock_sleep, tmp_path, monkeypatch):
        """Socket fails twice (FileNotFoundError), then succeeds on attempt 3."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "5")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "2")

        sock = str(tmp_path / "test.sock")
        call_count = {"n": 0}
        original_connect = None

        # We'll create the socket server but intercept connect to fail twice.
        class _FailThenSucceedSocket:
            """Wraps a real socket, failing the first N connects."""

            def __init__(self, *args, **kwargs):
                self._real = _orig_socket(*args, **kwargs)
                self._attempt = call_count["n"]
                call_count["n"] += 1

            def connect(self, addr):
                if self._attempt < 2:
                    raise ConnectionRefusedError("simulated failure")
                return self._real.connect(addr)

            def __getattr__(self, name):
                return getattr(self._real, name)

        import socket as socket_mod

        _orig_socket = socket_mod.socket

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                with patch.object(socket_mod, "socket", _FailThenSucceedSocket):
                    result = await _run_sync_in_async(
                        _send_via_socket, sock, {"content": "hello"}
                    )
                assert result is True
                # Server should have received the message on the 3rd attempt
                assert len(srv.received) == 1
                assert srv.received[0]["content"] == "hello"
            finally:
                await srv.stop()

        asyncio.run(_test())

        # Two backoff sleeps should have occurred (before attempts 2 and 3).
        assert mock_sleep.call_count == 2
        # Verify exponential backoff: base=2, delays = 2*2^0=2, 2*2^1=4
        assert mock_sleep.call_args_list[0][0][0] == 2.0
        assert mock_sleep.call_args_list[1][0][0] == 4.0

    @patch("ampa.notifications.time.sleep")
    def test_no_sleep_on_first_attempt_success(self, mock_sleep, tmp_path, monkeypatch):
        """When the first attempt succeeds, no sleep occurs."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "2")

        sock = str(tmp_path / "test.sock")

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    _send_via_socket, sock, {"content": "quick"}
                )
                assert result is True
            finally:
                await srv.stop()

        asyncio.run(_test())
        assert mock_sleep.call_count == 0


# ---------------------------------------------------------------------------
# Tests: final failure after exhausting retries
# ---------------------------------------------------------------------------


class TestFinalFailure:
    """All retries exhausted — verify ERROR log and dead-letter behavior."""

    @patch("ampa.notifications.time.sleep")
    def test_all_retries_exhausted_returns_false(
        self, mock_sleep, tmp_path, monkeypatch
    ):
        """Socket never available — _send_via_socket returns False after max retries."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "nonexistent.sock")
        result = _send_via_socket(sock, {"content": "fail"})
        assert result is False

        # Two backoff sleeps (between attempts 1-2 and 2-3; not after last).
        assert mock_sleep.call_count == 2
        # Verify exponential: base=1, delays = 1*2^0=1, 1*2^1=2
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0

    @patch("ampa.notifications.time.sleep")
    def test_final_failure_logged_at_error(
        self, mock_sleep, tmp_path, monkeypatch, caplog
    ):
        """After exhausting retries, an ERROR log is emitted."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "nonexistent.sock")
        with caplog.at_level(logging.WARNING, logger="ampa.notifications"):
            _send_via_socket(sock, {"content": "fail"})

        # Check that per-attempt WARNING logs exist
        warning_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "Send attempt" in r.getMessage()
        ]
        assert len(warning_msgs) == 3  # one per attempt

        # Check that the final ERROR log exists
        error_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "All 3 send attempts failed" in r.getMessage()
        ]
        assert len(error_msgs) == 1

    @patch("ampa.notifications.time.sleep")
    def test_each_attempt_logged_with_number(
        self, mock_sleep, tmp_path, monkeypatch, caplog
    ):
        """Each retry attempt is logged with its attempt number."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "nonexistent.sock")
        with caplog.at_level(logging.WARNING, logger="ampa.notifications"):
            _send_via_socket(sock, {"content": "fail"})

        warning_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "Send attempt" in r.getMessage()
        ]
        assert "1/3" in warning_msgs[0]
        assert "2/3" in warning_msgs[1]
        assert "3/3" in warning_msgs[2]

    @patch("ampa.notifications.time.sleep")
    def test_notify_dead_letters_after_retry_exhaustion(
        self, mock_sleep, tmp_path, monkeypatch
    ):
        """notify() dead-letters after _send_via_socket exhausts all retries."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "2")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "nonexistent.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)
        dl_file = str(tmp_path / "dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", dl_file)

        result = notify("Failure Test", "body", message_type="error")
        assert result is False

        # Dead-letter file should exist
        assert os.path.exists(dl_file)
        with open(dl_file) as f:
            record = json.loads(f.readline())
        assert record["reason"] == "Unix socket unreachable"
        assert "# Failure Test" in record["payload"]["content"]


# ---------------------------------------------------------------------------
# Tests: backoff timing pattern
# ---------------------------------------------------------------------------


class TestBackoffPattern:
    """Verify the exponential backoff delay sequence."""

    @patch("ampa.notifications.time.sleep")
    def test_backoff_doubles_each_attempt(self, mock_sleep, tmp_path, monkeypatch):
        """With base=2, delays should be 2, 4, 8, 16 for 5 attempts."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "5")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "2")

        sock = str(tmp_path / "nonexistent.sock")
        _send_via_socket(sock, {"content": "fail"})

        # 4 sleeps (between attempts, not after the last)
        assert mock_sleep.call_count == 4
        expected = [2.0, 4.0, 8.0, 16.0]
        actual = [call[0][0] for call in mock_sleep.call_args_list]
        assert actual == expected

    @patch("ampa.notifications.time.sleep")
    def test_single_retry_no_sleep(self, mock_sleep, tmp_path, monkeypatch):
        """With max_retries=1, there are no sleeps (only one attempt)."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "1")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "2")

        sock = str(tmp_path / "nonexistent.sock")
        _send_via_socket(sock, {"content": "fail"})

        assert mock_sleep.call_count == 0

    @patch("ampa.notifications.time.sleep")
    def test_custom_backoff_base(self, mock_sleep, tmp_path, monkeypatch):
        """With base=0.5, delays should be 0.5, 1.0, 2.0."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "4")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "0.5")

        sock = str(tmp_path / "nonexistent.sock")
        _send_via_socket(sock, {"content": "fail"})

        assert mock_sleep.call_count == 3
        expected = [0.5, 1.0, 2.0]
        actual = [call[0][0] for call in mock_sleep.call_args_list]
        assert actual == expected


# ---------------------------------------------------------------------------
# Tests: bot error responses trigger retry
# ---------------------------------------------------------------------------


class TestBotErrorRetry:
    """Bot returns ok=false — verify retries and eventual success/failure."""

    @patch("ampa.notifications.time.sleep")
    def test_bot_error_then_success(self, mock_sleep, tmp_path, monkeypatch):
        """Bot returns error twice, then succeeds on attempt 3."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "5")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "test.sock")
        responses = [
            {"ok": False, "error": "rate limited"},
            {"ok": False, "error": "rate limited"},
            {"ok": True},
        ]

        async def _test():
            srv = _FakeSocketServer(sock, responses=responses)
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    _send_via_socket, sock, {"content": "retry-me"}
                )
                assert result is True
                # Server should have received 3 messages (one per attempt)
                assert len(srv.received) == 3
            finally:
                await srv.stop()

        asyncio.run(_test())

        # Two sleeps between the 3 attempts
        assert mock_sleep.call_count == 2

    @patch("ampa.notifications.time.sleep")
    def test_bot_error_all_retries_exhausted(
        self, mock_sleep, tmp_path, monkeypatch, caplog
    ):
        """Bot always returns error — final failure logged at ERROR."""
        monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
        monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

        sock = str(tmp_path / "test.sock")

        async def _test():
            srv = _FakeSocketServer(sock, ok=False, error="channel not found")
            await srv.start()
            try:
                with caplog.at_level(logging.WARNING, logger="ampa.notifications"):
                    result = await _run_sync_in_async(
                        _send_via_socket, sock, {"content": "fail"}
                    )
                assert result is False
            finally:
                await srv.stop()

        asyncio.run(_test())

        error_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "All 3 send attempts failed" in r.getMessage()
        ]
        assert len(error_msgs) == 1
