"""AMPA package entry points and core heartbeat sender.

This module contains the same functionality as the top-level script but is
packaged under the `ampa` Python package so it can be imported in tests and
installed if needed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
from typing import Any, Dict, Optional, List
import tempfile
import urllib.parse

try:
    # optional dependency for .env file parsing
    from dotenv import load_dotenv, find_dotenv
except Exception:  # pragma: no cover - optional behavior
    load_dotenv = None
    find_dotenv = None

LOG = logging.getLogger("ampa.daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

__all__ = ["get_env_config", "run_once", "load_env"]

# Use notification helpers from ampa.notifications as the single source of truth.
from .notifications import (
    notify,
    dead_letter,
    _read_state,
    _write_state,
)
from .server import (
    ampa_heartbeat_failure_total,
    ampa_heartbeat_sent_total,
    ampa_last_heartbeat_timestamp_seconds,
)
from .server import start_metrics_server, register_scheduler


def _project_ampa_dir() -> str:
    """Return the per-project AMPA config directory.

    Path: ``<cwd>/.worklog/ampa/``.  The daemon is always spawned with
    ``cwd = projectRoot`` (see ampa.mjs) so ``os.getcwd()`` gives the
    correct project root at startup.
    """
    return os.path.join(os.getcwd(), ".worklog", "ampa")


def load_env() -> None:
    """Load environment overrides from .env when available.

    Resolution order (first file found wins):
      1. ``<projectRoot>/.worklog/ampa/.env``  (per-project config)
      2. ``<packageDir>/.env``                 (backward compat / single-project)
      3. ``<projectRoot>/.env``                (legacy repo-root fallback)
    """
    # Allow callers/tests to disable loading the .env file by setting
    # AMPA_LOAD_DOTENV=0.
    ampa_load = os.getenv("AMPA_LOAD_DOTENV", "1").lower()
    if ampa_load not in ("1", "true", "yes") or not load_dotenv:
        LOG.info("AMPA_LOAD_DOTENV=%r prevents loading .env files or dotenv not available", ampa_load)
        return

    # 1. Per-project .env  (<projectRoot>/.worklog/ampa/.env)
    project_env = os.path.join(_project_ampa_dir(), ".env")
    LOG.info("Checking for project .env at %s", project_env)
    if os.path.isfile(project_env):
        load_dotenv(project_env, override=True)
        LOG.info("Loaded environment overrides from %s", project_env)
        return

    # 1a. User-level XDG config .env (e.g. $XDG_CONFIG_HOME/opencode/.worklog/ampa/.env
    # or ~/.config/opencode/.worklog/ampa/.env).  This lets operators place
    # their token in a shared location instead of duplicating it per-project.
    xdg_env = os.path.expanduser("~/.config/opencode/.worklog/ampa/.env")
    LOG.info("Checking for XDG .env at %s", xdg_env)
    if os.path.isfile(xdg_env):
        load_dotenv(xdg_env, override=True)
        LOG.info("Loaded environment overrides from XDG .env")
        return

    # 2. Package .env (<packageDir>/.env)
    package_env = os.path.join(os.path.dirname(__file__), os.pardir, ".env")
    LOG.info("Checking for package .env at %s", package_env)
    if os.path.isfile(package_env):
        load_dotenv(package_env, override=True)
        LOG.info("Loaded environment overrides from package .env")
        return

    # 3. Legacy root .env (<repoRoot>/.env)
    legacy_env = os.path.join(os.getcwd(), ".env")
    LOG.info("Checking for legacy root .env at %s", legacy_env)
    if os.path.isfile(legacy_env):
        load_dotenv(legacy_env, override=True)
        LOG.info("Loaded environment overrides from legacy root .env")


def _check_required_files(interactive: bool = True) -> bool:
    """Ensure required AMPA files exist under <project>/.worklog/ampa/

    Auto-provision behavior (always-on):
    - If files exist, return True.
    - If files are missing, prefer copying from XDG per-user config
      (~/.config/opencode/.worklog/ampa/). If present, copy missing files.
    - Otherwise, create defaults from the packaged files shipped with AMPA.

    Returns True when files are present (or were created). Returns False
    only if all provisioning attempts fail.
    """
    project_dir = _project_ampa_dir()
    required_files = {
        "workflow.json": os.path.join(project_dir, "workflow.json"),
        "workflow-schema.json": os.path.join(project_dir, "workflow-schema.json"),
    }

    missing = []
    for name, path in required_files.items():
        if not os.path.exists(path):
            missing.append((name, path))

    if not missing:
        LOG.info("All required AMPA files present in %s", project_dir)
        return True

    # Attempt to copy from per-user XDG config if available
    xdg_dir = os.path.expanduser("~/.config/opencode/.worklog/ampa")
    xdg_available = any(os.path.exists(os.path.join(xdg_dir, name)) for name, _ in missing)
    if xdg_available:
        try:
            import shutil
            for name, path in missing:
                src = os.path.join(xdg_dir, name)
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copy2(src, path)
                    LOG.info("Copied %s from XDG global config to project", name)
            # Recompute missing after copy
            missing = [m for m in missing if not os.path.exists(m[1])]
            if not missing:
                return True
        except Exception:
            LOG.exception("Failed to copy required files from XDG config")

    # Fall back to packaged defaults
    try:
        package_dir = os.path.join(os.path.dirname(__file__), os.pardir, "docs", "workflow")
        import shutil
        for name, path in missing:
            src = os.path.join(package_dir, name)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(src, path)
                LOG.info("Created %s from packaged defaults", name)
        missing = [m for m in missing if not os.path.exists(m[1])]
        if not missing:
            return True
    except Exception:
        LOG.exception("Failed to create default workflow files from package")

    LOG.error("Failed to provision required AMPA files: %s", [m[0] for m in missing])
    return False


def get_env_config() -> Dict[str, Any]:
    """Read and validate environment configuration.

    Raises SystemExit (2) if AMPA_DISCORD_BOT_TOKEN is not set.
    """
    load_env()

    # Check for bot token — the new mechanism for Discord notifications.
    bot_token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
    if bot_token:
        bot_token = bot_token.strip().strip("'\"")
    if not bot_token:
        LOG.error("AMPA_DISCORD_BOT_TOKEN is not set; cannot send heartbeats")
        raise SystemExit(2)

    minutes_raw = os.getenv("AMPA_HEARTBEAT_MINUTES", "1")
    try:
        minutes = int(minutes_raw)
        if minutes <= 0:
            raise ValueError("must be positive")
    except Exception:
        LOG.warning("Invalid AMPA_HEARTBEAT_MINUTES=%r, falling back to 1", minutes_raw)
        minutes = 1

    return {"bot_token": bot_token, "minutes": minutes}


def _truncate_output(output: str, limit: int = 900) -> str:
    if len(output) <= limit:
        return output
    return output[:limit] + "\n... (truncated)"


def run_once(config: Dict[str, Any]) -> int:
    """Send a single heartbeat using the provided config.

    Returns 200 on success, 0 on skip/failure.
    """
    hostname = socket.gethostname()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    LOG.info("Evaluating whether to send heartbeat for host=%s", hostname)

    state_file = os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )
    state = _read_state(state_file)
    last_message_ts = None
    last_message_type = None
    last_heartbeat_ts = None
    try:
        if "last_message_ts" in state:
            last_message_ts = datetime.datetime.fromisoformat(state["last_message_ts"])
    except Exception:
        last_message_ts = None
    try:
        if "last_message_type" in state:
            last_message_type = state["last_message_type"]
    except Exception:
        last_message_type = None
    try:
        if "last_heartbeat_ts" in state:
            last_heartbeat_ts = datetime.datetime.fromisoformat(
                state["last_heartbeat_ts"]
            )
    except Exception:
        last_heartbeat_ts = None

    now = datetime.datetime.now(datetime.timezone.utc)

    # Only send the heartbeat if no non-heartbeat message was sent in the last 5 minutes.
    if last_message_ts is not None and last_message_type != "heartbeat":
        if (now - last_message_ts) < datetime.timedelta(minutes=5):
            LOG.info(
                "Skipping heartbeat: other message sent within last 5 minutes (last_message=%s)",
                state.get("last_message_ts"),
            )
            return 0

    # Send heartbeat via notification API and update heartbeat timestamp.
    # notify() updates the state file internally (last_message_ts /
    # last_message_type) so we only need to write last_heartbeat_ts here.
    ok = notify("AMPA Heartbeat", message_type="heartbeat")
    status = 200 if ok else 0
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_state(
            state_file,
            {
                "last_heartbeat_ts": now_iso,
                "last_message_ts": now_iso,
                "last_message_type": "heartbeat",
            },
        )
    except Exception:
        LOG.exception("Failed to update state after heartbeat")
    # Update Prometheus metrics
    try:
        if ok:
            ampa_heartbeat_sent_total.inc()
            ampa_last_heartbeat_timestamp_seconds.set(
                int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            )
        else:
            ampa_heartbeat_failure_total.inc()
    except Exception:
        LOG.debug("Failed to update Prometheus metrics")
    return status


def main() -> None:
    """Daemon entrypoint.

    Supports:
    - `--once`: send one heartbeat and exit
    - `--start-scheduler`: start the scheduler loop under the daemon runtime
    If neither flag is provided the default behaviour is to send a single heartbeat.
    """
    import argparse

    parser = argparse.ArgumentParser(description="AMPA daemon")
    parser.add_argument(
        "--once", action="store_true", help="Send one heartbeat and exit"
    )
    parser.add_argument(
        "--start-scheduler",
        action="store_true",
        help="Start the scheduler loop under the daemon runtime",
    )
    args = parser.parse_args()

    # Check for required AMPA files before proceeding
    if not _check_required_files(interactive=True):
        LOG.warning("Daemon startup aborted due to missing required files")
        return

    try:
        config = get_env_config()
    except SystemExit:
        # get_env_config logs and exits when misconfigured
        raise

    # Start observability server if requested. Honor AMPA_METRICS_PORT when set
    # to an integer > 0. If AMPA_METRICS_PORT is unset the default is 8000.
    try:
        _port_raw = os.getenv("AMPA_METRICS_PORT", "8000")
        _port = int(_port_raw)
    except Exception:
        _port = 8000
    try:
        if _port > 0:
            thr, bound = start_metrics_server(port=_port)
            LOG.info("Started metrics server on 127.0.0.1:%s", bound)
    except Exception:
        LOG.exception("Failed to start metrics server")

    # If requested, start scheduler as a long-running worker managed by daemon
    if args.start_scheduler or os.getenv("AMPA_RUN_SCHEDULER", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        try:
            # Import locally to avoid side-effects during test imports
            from . import scheduler

            LOG.info("Starting scheduler under daemon runtime")
            sched = scheduler.load_scheduler(command_cwd=os.getcwd())
            register_scheduler(sched)
            sched.run_forever()
            return
        except SystemExit:
            raise
        except Exception:
            LOG.exception("Failed to start scheduler from daemon")
            return

    # Default: send a single heartbeat
    LOG.info("Sending AMPA heartbeat once")
    try:
        run_once(config)
    except SystemExit:
        raise
    except Exception:
        LOG.exception("Error while sending heartbeat")


if __name__ == "__main__":
    main()
