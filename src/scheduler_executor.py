"""Command executor and scoring — extracted from scheduler.py.

Pure functions that handle command execution with timeout/notification
logic and scoring.  These are not scheduling decisions — they are the
mechanics of running a single command and computing priority scores.

Canonical imports::

    from ampa.scheduler_executor import default_executor, default_llm_probe, score_command
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import subprocess
import time
from typing import Optional, Tuple

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    requests = None

try:
    from . import daemon
    from . import notifications as notifications_module
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    daemon = importlib.import_module("ampa.daemon")
    notifications_module = importlib.import_module("ampa.notifications")

from .scheduler_types import (
    CommandSpec,
    CommandRunResult,
    RunResult,
    _utc_now,
)

LOG = logging.getLogger("ampa.scheduler")

# Seconds to wait after SIGTERM before escalating to SIGKILL.
_SIGTERM_GRACE_SECONDS = 5


def _run_command_with_graceful_timeout(
    command: str,
    timeout: int,
    command_cwd: Optional[str],
) -> subprocess.CompletedProcess:
    """Run a shell command with a SIGTERM → SIGKILL escalation on timeout.

    When *timeout* seconds elapse:

    1. ``SIGTERM`` is sent to the child process.
    2. Up to ``_SIGTERM_GRACE_SECONDS`` are allowed for graceful shutdown.
    3. If the process is still alive, ``SIGKILL`` is sent.
    4. ``subprocess.TimeoutExpired`` is raised so the caller can record
       exit code 124 and clear any ``running=True`` flags.
    """
    proc = subprocess.Popen(  # nosec - shell execution is explicit configuration
        command,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=command_cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=command,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        LOG.warning(
            "Delegation command timed out after %ss — sending SIGTERM to process group (pgid=%s)",
            timeout,
            proc.pid,
        )
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            LOG.exception(
                "Failed to send SIGTERM to delegation process group pgid=%s", proc.pid
            )
        stdout_val: str = ""
        stderr_val: str = ""
        try:
            stdout_val, stderr_val = proc.communicate(timeout=_SIGTERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            LOG.warning(
                "Delegation process group pgid=%s did not exit after SIGTERM — escalating to SIGKILL",
                proc.pid,
            )
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                LOG.exception(
                    "Failed to send SIGKILL to delegation process group pgid=%s", proc.pid
                )
            try:
                stdout_val, stderr_val = proc.communicate(timeout=5)
            except Exception:
                pass
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout_val,
            stderr=stderr_val,
        )


def default_llm_probe(url: str) -> bool:
    if requests is None:
        LOG.debug("requests missing; assuming LLM unavailable")
        return False
    try:
        resp = requests.get(url, timeout=2)
        return resp.status_code < 500
    except Exception:
        return False


def default_executor(spec: CommandSpec, command_cwd: Optional[str] = None) -> RunResult:
    if spec.command_type == "heartbeat":
        start = _utc_now()
        try:
            config = daemon.get_env_config()
            status = daemon.run_once(config)
        except SystemExit as exc:
            status = getattr(exc, "code", 1) or 1
        end = _utc_now()
        return CommandRunResult(
            start_ts=start,
            end_ts=end,
            exit_code=int(status),
            output="heartbeat",
        )
    start = _utc_now()
    # Determine an execution timeout in seconds.
    # Priority (highest -> lowest):
    # 1. CommandSpec.max_runtime_minutes (per-command override)
    # 2. DELEGATION_TIMEOUT_SECONDS env var (canonical name for delegation/opencode)
    # 3. AMPA_DELEGATION_OPENCODE_TIMEOUT env var (legacy alias, preserved for
    #    backward compatibility)
    # 4. Global AMPA_CMD_TIMEOUT_SECONDS default
    timeout = None
    try:
        default_cmd_timeout = int(os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "3600"))
    except Exception:
        default_cmd_timeout = 3600
    if spec.max_runtime_minutes is not None:
        timeout = max(1, int(spec.max_runtime_minutes * 60))
    else:
        # Enforce a default timeout for delegation flows and for commands that
        # spawn `opencode run` to avoid leaving the scheduler marked running
        # indefinitely when a child process hangs. Non-opencode commands keep
        # the previous behaviour unless explicitly configured.
        try:
            # DELEGATION_TIMEOUT_SECONDS is the canonical name; fall back to
            # the legacy AMPA_DELEGATION_OPENCODE_TIMEOUT alias for backward
            # compatibility, then to the global default.
            raw_timeout = (
                os.getenv("DELEGATION_TIMEOUT_SECONDS")
                or os.getenv("AMPA_DELEGATION_OPENCODE_TIMEOUT")
            )
            delegate_timeout = int(raw_timeout) if raw_timeout else default_cmd_timeout
        except Exception:
            delegate_timeout = default_cmd_timeout
        if spec.command_type == "delegation" or "opencode run" in (spec.command or ""):
            timeout = max(1, int(delegate_timeout))

    is_delegation_cmd = spec.command_type == "delegation" or "opencode run" in (
        spec.command or ""
    )
    LOG.info("Starting command %s (timeout=%s)", spec.command_id, timeout)
    try:
        if is_delegation_cmd and timeout is not None:
            # Use Popen-based graceful termination (SIGTERM → SIGKILL) for
            # delegation and opencode-run commands so they receive a chance to
            # clean up before being force-killed.
            result = _run_command_with_graceful_timeout(
                spec.command, timeout, command_cwd
            )
        else:
            result = subprocess.run(  # nosec - shell execution is explicit configuration
                spec.command,
                shell=True,
                check=False,
                timeout=timeout,
                text=True,
                capture_output=True,
                cwd=command_cwd,
            )
        end = _utc_now()
    except subprocess.TimeoutExpired as e:
        # Normalize timeouts to exit code 124 and notify operators via
        # Discord when configured. Return a CompletedProcess-like object so
        # the rest of the function can treat the result uniformly.
        end = _utc_now()
        out = getattr(e, "output", None) or ""
        err = getattr(e, "stderr", None) or ""
        timeout_reason = (
            f"Command {spec.command_id} timed out after {timeout}s: {spec.command}"
        )
        # Log at WARNING so operators can find timeout events without scanning
        # verbose DEBUG output. For delegation/opencode commands, include a
        # note indicating where a wl comment would be posted (ampa/scheduler.py
        # start_command) for future implementation by a follow-up task.
        if is_delegation_cmd:
            LOG.warning(
                "Delegation command %s timed out after %ss — running flag will be "
                "cleared by Scheduler._record_run. "
                "A wl worklog comment should be posted at ampa/scheduler.py:"
                "start_command for work item traceability.",
                spec.command_id,
                timeout,
            )
        else:
            LOG.warning(
                "Command %s timed out after %s seconds",
                spec.command_id,
                timeout,
            )
        try:
            notifications_module.notify(
                title=(spec.title or spec.command)[:128],
                body=timeout_reason,
                message_type="error",
            )
        except Exception:
            LOG.exception("Failed to send timeout notification")
        result = subprocess.CompletedProcess(
            args=spec.command,
            returncode=124,
            stdout=out,
            stderr=err,
        )

    LOG.info(
        "Finished command %s exit=%s duration=%.2fs",
        spec.command_id,
        result.returncode,
        (end - start).total_seconds(),
    )
    output = ""
    if getattr(result, "stdout", None):
        output += result.stdout
    if getattr(result, "stderr", None):
        output += result.stderr
    return CommandRunResult(
        start_ts=start,
        end_ts=end,
        exit_code=result.returncode,
        output=output.strip(),
    )


def score_command(
    spec: CommandSpec,
    now: dt.datetime,
    last_run: Optional[dt.datetime],
    priority_weight: float,
) -> Tuple[float, float]:
    desired_interval = max(1.0, spec.frequency_minutes * 60.0)
    if last_run is None:
        time_since_last = now.timestamp()
    else:
        time_since_last = (now - last_run).total_seconds()
    lateness = time_since_last - desired_interval
    normalized_lateness = max(lateness / desired_interval, 0.0)
    priority_factor = 1.0 + max(priority_weight, 0.0) * spec.priority
    return normalized_lateness * priority_factor, normalized_lateness
