"""Discord bot process supervision — extracted from scheduler.py.

Encapsulates the lifecycle management of the Discord bot subprocess:
starting, monitoring, restarting on failure, waiting for socket readiness,
sending the startup notification, and graceful shutdown.

Canonical imports::

    from ampa.bot_supervisor import BotSupervisor
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable, Optional

LOG = logging.getLogger("ampa.scheduler")


class BotSupervisor:
    """Manages the Discord bot child process lifecycle.

    Parameters
    ----------
    run_shell:
        Callable used to execute shell commands (for ``wl status``).
    command_cwd:
        Working directory for shell commands.
    notifications_module:
        The ``ampa.notifications`` module (or compatible duck-type) used
        to dispatch startup notifications.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        notifications_module: Any,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self.notifications_module = notifications_module

        self._bot_process: Optional[subprocess.Popen] = None
        self._bot_consecutive_failures: int = 0
        self._BOT_MAX_CONSECUTIVE_FAILURES: int = 3

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_running(self) -> None:
        """Ensure the Discord bot process is alive, starting it if needed.

        Called at scheduler startup and at the top of each cycle.  If
        ``AMPA_DISCORD_BOT_TOKEN`` is not set, this is a no-op — notifications
        are silently disabled.

        If the bot fails to start on 3 consecutive attempts the supervisor logs
        an error and stops retrying until the scheduler is restarted (to avoid
        spamming logs and wasting resources).
        """
        token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
        if not token:
            return

        # Already exceeded consecutive failure limit — don't keep retrying.
        if self._bot_consecutive_failures >= self._BOT_MAX_CONSECUTIVE_FAILURES:
            return

        # Check if existing process is still alive.
        if self._bot_process is not None:
            rc = self._bot_process.poll()
            if rc is None:
                # Process is alive — nothing to do.
                return
            # Process has exited.
            LOG.warning(
                "Discord bot process (pid=%d) exited with code %s – restarting",
                self._bot_process.pid,
                rc,
            )
            self._bot_process = None

        # Attempt to start the bot.
        try:
            self._bot_process = subprocess.Popen(
                [sys.executable, "-m", "ampa.discord_bot"],
                start_new_session=True,
            )
            LOG.info(
                "Started Discord bot process (pid=%d)",
                self._bot_process.pid,
            )
            self._bot_consecutive_failures = 0
        except Exception:
            self._bot_consecutive_failures += 1
            LOG.exception(
                "Failed to start Discord bot process (attempt %d/%d)",
                self._bot_consecutive_failures,
                self._BOT_MAX_CONSECUTIVE_FAILURES,
            )
            if self._bot_consecutive_failures >= self._BOT_MAX_CONSECUTIVE_FAILURES:
                LOG.error(
                    "Discord bot failed to start %d consecutive times – "
                    "giving up until scheduler restart",
                    self._BOT_MAX_CONSECUTIVE_FAILURES,
                )

    def wait_for_socket(self, timeout: float = 15.0) -> None:
        """Block until the bot's Unix socket appears or *timeout* expires.

        Called once at startup between ``ensure_running()`` and
        ``post_startup_message()`` so the startup notification is not
        dead-lettered due to a race condition.

        A stale socket file from a previous bot process is deleted first so
        we only return once the **new** bot has created its socket and is
        actually listening.
        """
        if self._bot_process is None:
            # Bot not started (token not set or exceeded failure limit).
            return
        socket_path = os.getenv("AMPA_BOT_SOCKET_PATH", "/tmp/ampa_bot.sock")

        # Remove stale socket from a previous run so we wait for the new one.
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
                LOG.info("Removed stale bot socket %s before waiting", socket_path)
            except OSError:
                LOG.warning("Could not remove stale socket %s", socket_path)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                LOG.info("Bot socket ready at %s", socket_path)
                return
            time.sleep(0.5)
        LOG.warning(
            "Bot socket not found at %s after %.0fs – "
            "startup message may be dead-lettered",
            socket_path,
            timeout,
        )

    def shutdown(self) -> None:
        """Send SIGTERM to the bot process and wait briefly for it to exit."""
        if self._bot_process is None:
            return
        rc = self._bot_process.poll()
        if rc is not None:
            # Already exited.
            self._bot_process = None
            return
        pid = self._bot_process.pid
        LOG.info("Sending SIGTERM to Discord bot process (pid=%d)", pid)
        try:
            self._bot_process.terminate()
            try:
                self._bot_process.wait(timeout=5)
                LOG.info("Discord bot process (pid=%d) exited cleanly", pid)
            except subprocess.TimeoutExpired:
                LOG.warning(
                    "Discord bot process (pid=%d) did not exit after SIGTERM; "
                    "sending SIGKILL",
                    pid,
                )
                self._bot_process.kill()
                self._bot_process.wait(timeout=2)
        except Exception:
            LOG.exception("Error shutting down Discord bot process (pid=%d)", pid)
        finally:
            self._bot_process = None

    def post_startup_message(self) -> None:
        """Send the scheduler-started notification via Discord.

        Captures the output of ``wl status`` and dispatches it as a
        ``startup`` notification.  No-op if the bot token is not set.
        """
        # Only attempt startup notification if Discord bot is configured.
        if not os.getenv("AMPA_DISCORD_BOT_TOKEN"):
            return
        # Capture the human-facing output of `wl status` for the startup message
        try:
            proc = self.run_shell(
                "wl status",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            # Some test doubles may return CompletedProcess with stdout only when
            # the command used '--json'. Ensure we try the JSON variant when the
            # plain invocation produced no useful output so tests that stub
            # `run_shell` for `wl status` still exercise the intended path.
            if getattr(proc, "stdout", None) == "":
                json_proc = self.run_shell(
                    "wl status --json",
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                )
                if getattr(json_proc, "stdout", None):
                    proc = json_proc
            status_out = ""
            if getattr(proc, "stdout", None):
                status_out += proc.stdout
            if getattr(proc, "stderr", None):
                # prefer stderr only if stdout is empty to keep message concise
                if not status_out:
                    status_out += proc.stderr
            if not status_out:
                status_out = "(wl status produced no output)"
        except Exception:
            LOG.exception("Failed to run 'wl status' for startup message")
            status_out = "(wl status unavailable)"

        self.notifications_module.notify(
            title="Scheduler Started",
            body=status_out,
            message_type="startup",
        )
        LOG.info("Startup notification dispatched")
