"""Global pytest fixtures for the ampa test suite."""

import os

import pytest


@pytest.fixture(autouse=True)
def _limit_notification_retries(monkeypatch):
    """Cap notification retries to 1 for every test.

    The retry/backoff logic in ampa.notifications._send_via_socket() defaults
    to 10 retries with exponential backoff (total ~17 min of sleeping).  In CI
    the Unix-domain socket is absent so every unprotected notify() call would
    sleep through the full retry budget and hit the 30-second per-test timeout.

    Setting AMPA_MAX_RETRIES=1 ensures a single fast failure instead.
    """
    monkeypatch.setenv("AMPA_MAX_RETRIES", "1")
