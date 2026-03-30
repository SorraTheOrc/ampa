import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ampa import conversation_manager
from ampa import session_block
from ampa import responder


def test_start_and_resume(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)
    monkeypatch.setenv("AMPA_RESPONDER_URL", "http://localhost:8081/respond")

    session_id = "s-123"
    prompt = "Please confirm this change"

    captured = {}

    def fake_notify(title, body="", message_type="other", *, payload=None):
        captured["title"] = title
        captured["body"] = body
        captured["message_type"] = message_type
        return True

    monkeypatch.setattr(session_block.notifications_module, "notify", fake_notify)

    meta = conversation_manager.start_conversation(
        session_id, prompt, {"work_item": "WL-1"}
    )
    assert meta["session"] == session_id
    assert meta["state"] == "waiting_for_input"

    # ensure pending prompt file exists and contains full payload
    prompt_file = meta["prompt_file"]
    assert os.path.exists(prompt_file)
    with open(prompt_file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["prompt_text"] == prompt
    assert payload["choices"] == []
    assert payload["context"] == []
    assert payload["session_id"] == session_id
    assert payload["stamp"]
    assert captured["message_type"] == "waiting_for_input"
    body = captured["body"]
    assert "Session: s-123" in body
    assert "Work item: WL-1" in body
    assert "Reason: Please confirm this change" in body
    assert "Call to action:" in body
    assert "Pending prompt file:" in body
    assert "Responder endpoint: http://localhost:8081/respond" in body
    assert "Persisted prompt path:" in body

    # resume
    res = conversation_manager.resume_session(session_id, "yes")
    assert res["status"] == "resumed"
    assert res["session"] == session_id
    assert res["prompt_text"] == prompt
    assert res["choices"] == []
    assert res["context"] == []
    assert os.path.exists(os.path.join(tool_dir, "events.jsonl"))


def test_resume_no_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(conversation_manager.NotFoundError):
        conversation_manager.resume_session("no-such", "x")


def test_resume_with_sdk_client(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    class DummySDK:
        def __init__(self):
            self.calls = []

        def start_conversation(self, session_id, prompt, metadata):
            self.calls.append(("start", session_id, prompt, metadata))

        def resume_session(self, session_id, response, metadata):
            self.calls.append(("resume", session_id, response, metadata))

    sdk = DummySDK()
    session_id = "s-sdk"
    conversation_manager.start_conversation(
        session_id, "prompt", {"work_item": "WL-1", "sdk_client": sdk}
    )
    res = conversation_manager.resume_session(session_id, "ok", {"sdk_client": sdk})

    assert res["status"] == "resumed"
    assert sdk.calls[0][0] == "start"
    assert sdk.calls[1][0] == "resume"


def test_resume_invalid_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-foo"
    # create a pending prompt file but set session state to something else
    meta = {
        "session": session_id,
        "session_id": session_id,
        "work_item": None,
        "summary": "x",
        "prompt_text": "full prompt",
        "choices": ["a", "b"],
        "context": [{"role": "user", "content": "hi"}],
        "state": "waiting_for_input",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "stamp": "1",
    }
    prompt_file = os.path.join(str(tmp_path), f"pending_prompt_{session_id}_1.json")
    with open(prompt_file, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    # write session state as completed
    session_block.set_session_state(session_id, "completed")

    with pytest.raises(conversation_manager.InvalidStateError):
        conversation_manager.resume_session(session_id, "ok")


def test_resume_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-timeout"
    created_at = (datetime.utcnow() - timedelta(days=2)).isoformat() + "Z"
    meta = {
        "session": session_id,
        "session_id": session_id,
        "work_item": None,
        "summary": "x",
        "prompt_text": "full prompt",
        "choices": ["a", "b"],
        "context": [{"role": "user", "content": "hi"}],
        "state": "waiting_for_input",
        "created_at": created_at,
        "stamp": "1",
    }
    prompt_file = os.path.join(str(tmp_path), f"pending_prompt_{session_id}_1.json")
    with open(prompt_file, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    session_block.set_session_state(session_id, "waiting_for_input")

    with pytest.raises(conversation_manager.TimedOutError):
        conversation_manager.resume_session(session_id, "ok", timeout_seconds=10)


def test_responder_payload_resume(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    session_id = "s-responder"
    prompt = "Approve deploy?"
    choices = ["yes", "no"]
    context = [{"role": "user", "content": "ship it"}]
    conversation_manager.start_conversation(
        session_id,
        prompt,
        {"work_item": "WL-2", "choices": choices, "context": context},
    )

    payload = {"session_id": session_id, "response": "yes"}
    result = responder.resume_from_payload(payload)

    assert result["status"] == "resumed"
    assert result["session"] == session_id
    assert result["prompt_text"] == prompt
    assert result["choices"] == choices
    assert result["context"] == context


def test_responder_payload_action_accept(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    session_id = "s-responder-action"
    prompt = "Approve deploy?"
    conversation_manager.start_conversation(
        session_id,
        prompt,
        {"work_item": "WL-3"},
    )

    payload = {"session_id": session_id, "action": "accept"}
    result = responder.resume_from_payload(payload)

    assert result["status"] == "resumed"
    assert result["session"] == session_id
    assert result["response"] == "accept"


def test_detect_and_surface_blocking_prompt_persists_state(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)
    monkeypatch.setenv("AMPA_RESPONDER_URL", "http://localhost:8081/respond")

    captured = {}

    def fake_notify(title, body="", message_type="other", *, payload=None):
        captured["title"] = title
        captured["body"] = body
        captured["message_type"] = message_type
        return True

    monkeypatch.setattr(session_block.notifications_module, "notify", fake_notify)

    session_id = "s-blocked"
    prompt = "Approve change?"

    meta = session_block.detect_and_surface_blocking_prompt(
        session_id,
        "WL-44",
        prompt,
        choices=["yes", "no"],
        context=[{"role": "user", "content": "review"}],
    )

    prompt_file = meta["prompt_file"]
    assert os.path.exists(prompt_file)
    with open(prompt_file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["prompt_text"] == prompt
    assert payload["choices"] == ["yes", "no"]
    assert payload["context"] == [{"role": "user", "content": "review"}]
    assert payload["session_id"] == session_id

    state_file = os.path.join(tool_dir, f"session_{session_id}.json")
    assert os.path.exists(state_file)
    with open(state_file, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    assert state["state"] == "waiting_for_input"

    assert captured["message_type"] == "waiting_for_input"
    body = captured["body"]
    assert "Session: s-blocked" in body
    assert "Work item: WL-44" in body
    assert "Reason: Approve change?" in body
