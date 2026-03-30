import datetime
import json

from typing import Any, cast

import ampa.daemon as daemon
from ampa.daemon import get_env_config, run_once
from ampa import notifications


def write_state(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def read_state(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_heartbeat_skipped_when_other_message_since_last_heartbeat(
    monkeypatch, tmp_path
):
    state_file = tmp_path / "ampa_state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    # other message 30s ago -> skip heartbeat
    write_state(
        state_file,
        {
            "last_message_ts": (now - datetime.timedelta(seconds=30)).isoformat(),
            "last_message_type": "other",
        },
    )

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    called = {"count": 0}

    def fake_notify(title, body="", message_type="other", **kwargs):
        called["count"] += 1
        return True

    monkeypatch.setattr("ampa.daemon.notify", fake_notify)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 0
    assert called["count"] == 0


def test_heartbeat_sent_and_updates_state(monkeypatch, tmp_path):
    state_file = tmp_path / "ampa_state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    # last message was 6 minutes ago -> heartbeat should send
    write_state(
        state_file,
        {
            "last_message_ts": (now - datetime.timedelta(minutes=6)).isoformat(),
            "last_message_type": "other",
        },
    )

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    def fake_notify(title, body="", message_type="other", **kwargs):
        return True

    monkeypatch.setattr("ampa.daemon.notify", fake_notify)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 200

    st = read_state(state_file)
    assert st.get("last_message_type") == "heartbeat"
    assert "last_heartbeat_ts" in st


def test_initial_heartbeat_when_no_state(monkeypatch, tmp_path):
    state_file = tmp_path / "ampa_state.json"
    if state_file.exists():
        state_file.unlink()

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    def fake_notify(title, body="", message_type="other", **kwargs):
        return True

    monkeypatch.setattr("ampa.daemon.notify", fake_notify)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 200

    st = read_state(state_file)
    assert st.get("last_message_type") == "heartbeat"
    assert "last_heartbeat_ts" in st


def test_build_command_payload_includes_output():
    payload = cast(Any, notifications).build_command_payload(
        "host",
        "2026-01-01T00:00:00+00:00",
        "wl-in_progress",
        "in progress output",
        0,
    )
    content = payload["content"]
    # command_id and exit_code are technical fields and should not appear
    # in the human-facing Discord payload. Only the output summary should be
    # present.
    assert "command_id:" not in content
    assert "exit_code:" not in content
    assert "in progress output" in content
