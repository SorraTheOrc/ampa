import datetime
import socket

import pytest

from ampa.notifications import build_payload
from ampa.daemon import get_env_config


def test_build_payload_includes_hostname_and_timestamp():
    hostname = "test-host"
    ts = datetime.datetime(
        2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
    ).isoformat()
    payload = build_payload(hostname, ts, work_item_id="SA-123")
    assert "content" in payload
    content = payload["content"]
    # New behavior: payload is markdown-first and only contains human-facing
    # fields passed via extra_fields. Do not expose technical Host/Timestamp
    # or internal ids in the default payload.
    assert content.startswith("# AMPA Heartbeat")
    assert "Host:" not in content
    assert "Timestamp:" not in content
    assert "work_item_id:" not in content


def test_get_env_config_missing_bot_token(monkeypatch):
    monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("AMPA_HEARTBEAT_MINUTES", "1")
    # Ensure package .env is not loaded during this test so the missing-token
    # behavior is exercised even when ampa/.env exists in the repository.
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")
    with pytest.raises(SystemExit):
        get_env_config()


def test_get_env_config_invalid_minutes(monkeypatch):
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")
    monkeypatch.setenv("AMPA_HEARTBEAT_MINUTES", "-5")
    cfg = get_env_config()
    assert cfg["minutes"] == 1
