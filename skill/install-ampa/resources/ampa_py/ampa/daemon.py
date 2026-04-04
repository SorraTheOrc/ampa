"""Daemon entrypoint copied from src/ampa/daemon.py for installer package.

This is a minimal copy to allow the installed package to be importable when the
installer starts the packaged ampa daemon. It mirrors the runtime file from the
repo so the installed venv can import ampa.daemon.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("ampa.daemon")


def main() -> None:
    # Minimal startup sequence for the packaged daemon. Real implementation
    # lives in repo/src/ampa/daemon.py; installer copies this so the venv can
    # import ampa.daemon without ModuleNotFoundError.
    logging.basicConfig(level=logging.INFO)
    log.info("Packaged ampa daemon started (minimal stub)")
    # The real installer will provide AMPA_RUN_SCHEDULER etc. We simply echo
    # environment keys to help debugging in the packaged environment.
    for k in ["AMPA_RUN_SCHEDULER", "AMPA_WORKFLOW_DESCRIPTOR", "AMPA_DISCORD_BOT_TOKEN"]:
        log.info("%s=%s", k, os.environ.get(k))


if __name__ == "__main__":
    main()
