"""Global pytest fixtures for the ampa test suite."""

# Ensure the package source directory is on sys.path so tests running from
# the repository root can import the package without an editable/installed
# installation. This supports the src/ layout used by this project.
import os
import sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


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
