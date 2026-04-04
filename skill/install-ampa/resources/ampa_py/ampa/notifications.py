"""Notifications helpers copied into installer resources.

Provides the minimal names expected by imports in daemon/discord modules so the
packaged ampa can be imported by the installer's venv. Full behaviour is
implemented in src/ampa/notifications.py; this file is a simple pass-through
to avoid ModuleNotFoundError during installer startup.
"""
from __future__ import annotations

import logging

log = logging.getLogger("ampa.notifications")


def notify_startup(*, message: str) -> None:
    log.info("notify_startup: %s", message)


def notify_shutdown(*, message: str) -> None:
    log.info("notify_shutdown: %s", message)
