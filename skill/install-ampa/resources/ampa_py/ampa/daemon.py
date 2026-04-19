"""Daemon entrypoint for the installed AMPA plugin package.

This module is the entrypoint used by ``wl ampa start`` when the AMPA plugin
is installed globally or per-project. It handles .env loading and delegates
to the scheduler or scheduler_cli module.

The full source lives in ``src/ampa/daemon.py``; this copy is deployed by the
installer into ``.worklog/plugins/ampa_py/ampa/daemon.py``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("ampa.daemon")

try:
    from dotenv import load_dotenv, find_dotenv
except Exception:
    load_dotenv = None
    find_dotenv = None


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
      2. User-level XDG config (e.g. $XDG_CONFIG_HOME/opencode/.worklog/ampa/.env)
      3. ``<packageDir>/.env``                 (backward compat / single-project)
      4. ``<projectRoot>/.env``                (legacy repo-root fallback)
    """
    ampa_load = os.getenv("AMPA_LOAD_DOTENV", "1").lower()
    if ampa_load not in ("1", "true", "yes") or not load_dotenv:
        log.info("AMPA_LOAD_DOTENV=%r prevents loading .env files or dotenv not available", ampa_load)
        return

    # 1. Per-project .env  (<projectRoot>/.worklog/ampa/.env)
    project_env = os.path.join(_project_ampa_dir(), ".env")
    log.info("Checking for project .env at %s", project_env)
    if os.path.isfile(project_env):
        load_dotenv(project_env, override=True)
        log.info("Loaded environment overrides from %s", project_env)
        return

    # 1a. User-level XDG config .env
    try:
        xdg_base = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        xdg_env = os.path.join(xdg_base, "opencode", ".worklog", "ampa", ".env")
        log.info("Checking for XDG user .env at %s", xdg_env)
        if os.path.isfile(xdg_env):
            load_dotenv(xdg_env, override=True)
            log.info("Loaded environment overrides from %s", xdg_env)
            return
    except Exception:
        pass

    # 2. Package-local .env (backward compat for single-project / local installs)
    pkg_env_path = os.path.join(os.path.dirname(__file__), ".env")
    log.info("Checking for package-local .env at %s", pkg_env_path)
    if find_dotenv:
        found = find_dotenv(pkg_env_path, usecwd=True)
        if found:
            load_dotenv(found, override=True)
            log.info("Loaded environment overrides from %s", found)
            return
    elif os.path.isfile(pkg_env_path):
        load_dotenv(pkg_env_path, override=True)
        log.info("Loaded environment overrides from %s", pkg_env_path)
        return

    # 3. Legacy repo-root .env
    root_env = os.path.join(os.getcwd(), ".env")
    log.info("Checking for repo-root .env at %s", root_env)
    if os.path.isfile(root_env):
        load_dotenv(root_env, override=True)
        log.info("Loaded environment overrides from %s", root_env)
        return

    log.info("No .env file found by load_env; proceeding with existing environment")


def main() -> None:
    """Daemon entrypoint.

    Supports:
    - ``--once``: send one heartbeat and exit
    - ``--start-scheduler``: start the scheduler loop under the daemon runtime

    If neither flag is provided the default behaviour is to send a single heartbeat.
    """
    parser = argparse.ArgumentParser(description="AMPA daemon")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    parser.add_argument("--start-scheduler", action="store_true", help="Start the scheduler loop under the daemon runtime")
    args = parser.parse_args()

    # Load .env before checking any configuration so vars like
    # AMPA_DISCORD_BOT_TOKEN are available to the scheduler.
    load_env()

    # Log key environment variables after loading .env (mask sensitive values)
    for k in ["AMPA_RUN_SCHEDULER", "AMPA_WORKFLOW_DESCRIPTOR", "AMPA_DISCORD_BOT_TOKEN"]:
        val = os.environ.get(k)
        if val and k == "AMPA_DISCORD_BOT_TOKEN" and len(val) > 8:
            val = val[:4] + "..." + val[-4:]
        log.info("%s=%s", k, val)

    # Start scheduler when requested (via --start-scheduler flag or
    # AMPA_RUN_SCHEDULER env var, which the JS plugin sets for ``wl ampa start``).
    if args.start_scheduler or os.getenv("AMPA_RUN_SCHEDULER", "").lower() in ("1", "true", "yes"):
        try:
            from . import scheduler
            log.info("Starting scheduler under daemon runtime")
            sched = scheduler.load_scheduler(command_cwd=os.getcwd())
            sched.run_forever()
            return
        except ImportError:
            log.info("scheduler module not available in installed package; falling back to scheduler_cli")
            # Fall through to scheduler_cli below
        except SystemExit:
            raise
        except Exception:
            log.exception("Failed to start scheduler from daemon")
            return

        # Fallback: run scheduler_cli as a module (works with the minimal
        # installed package that may not include the full scheduler module).
        # Pass 'run' subcommand since scheduler_cli expects a subcommand arg.
        try:
            from . import scheduler_cli
            log.info("Starting scheduler_cli under daemon runtime")
            scheduler_cli.main(['run'])
            return
        except SystemExit:
            raise
        except Exception:
            log.exception("Failed to start scheduler_cli from daemon")
            return

    # Default: send a single heartbeat
    log.info("Sending AMPA heartbeat once")
    bot_token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
    if not bot_token:
        log.error("AMPA_DISCORD_BOT_TOKEN is not set; cannot send heartbeats")
        sys.exit(2)


if __name__ == "__main__":
    main()