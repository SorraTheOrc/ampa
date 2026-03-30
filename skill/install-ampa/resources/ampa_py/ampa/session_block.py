"""session_block: packaged copy for installer resources.

This file is a copy of the fixed implementation used by the installer so
that projects without a local `ampa/` directory still get a working
package when the installer copies bundled resources.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Dict, Any, Optional

try:
    # Use package-relative import to avoid circular import when ampa package
    # is imported by other modules in the installed layout.
    from . import notifications as notifications_module
except Exception:  # pragma: no cover - optional dependency
    notifications_module = None

LOG = logging.getLogger("session_block")


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    default = os.path.join(tempfile.gettempdir(), "opencode_tool_output")
    return default


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        LOG.exception("Failed to create tool-output dir=%s", path)


def _excerpt_text(text: Optional[str], limit: int = 500) -> str:
    if not text:
        return ""
    one = " ".join(str(text).split())
    if len(one) <= limit:
        return one
    return one[:limit].rstrip() + "..."


def emit_internal_event(event_type: str, payload: Dict[str, Any]) -> str:
    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)
    events_path = os.path.join(out_dir, "events.jsonl")
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        "payload": payload,
    }
    try:
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
        LOG.info("Emitted internal event=%s to %s", event_type, events_path)
    except Exception:
        LOG.exception("Failed to write internal event to %s", events_path)
    return events_path


def set_session_state(session_id: str, state: str) -> str:
    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)
    state_path = os.path.join(out_dir, f"session_{session_id}.json")
    payload = {
        "session": session_id,
        "state": state,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        LOG.info(
            "Wrote session state for session=%s state=%s path=%s",
            session_id,
            state,
            state_path,
        )
    except Exception:
        LOG.exception("Failed to write session state to %s", state_path)
    return state_path


def _waiting_actions_text() -> str:
    return os.getenv(
        "AMPA_WAITING_FOR_INPUT_ACTIONS",
        "Auto-accept, auto-decline, or respond via the responder endpoint.",
    )


def _responder_endpoint_url() -> str:
    return os.getenv("AMPA_RESPONDER_URL", "http://localhost:8081/respond")


def _send_waiting_for_input_notification(metadata: Dict[str, Any]) -> Optional[int]:
    if notifications_module is None:
        LOG.warning("ampa.notifications is unavailable; cannot send notification")
        return None
    try:
        hostname = os.uname().nodename
    except Exception:
        hostname = "(unknown host)"
    actions = _waiting_actions_text()
    summary = metadata.get("summary") or "(no summary)"
    work_item = metadata.get("work_item") or "(none)"
    session_id = metadata.get("session") or "(unknown)"
    prompt_file = metadata.get("prompt_file") or "(unknown)"
    pending_prompt_file = metadata.get("pending_prompt_file") or prompt_file
    tool_dir = metadata.get("tool_output_dir") or _tool_output_dir()
    responder_url = _responder_endpoint_url()
    call_to_action = f"Respond now: {responder_url}"
    output = (
        "Session is waiting for input\n"
        f"Session: {session_id}\n"
        f"Work item: {work_item}\n"
        f"Reason: {summary}\n"
        f"Actions: {actions}\n"
        f"Call to action: {call_to_action}\n"
        f"Responder endpoint: {responder_url}\n"
        f"Persisted prompt path: {pending_prompt_file}\n"
        f"Pending prompt file: {pending_prompt_file}\n"
        f"Tool output dir: {tool_dir}"
    )
    try:
        ok = notifications_module.notify(
            "Session Waiting For Input",
            output,
            message_type="waiting_for_input",
        )
        return 200 if ok else 0
    except Exception:
        LOG.exception("Failed to send waiting_for_input notification")
        return None


def detect_and_surface_blocking_prompt(
    session_id: str,
    work_item_id: Optional[str],
    prompt_text: str,
    *,
    choices: Optional[Any] = None,
    context: Optional[Any] = None,
):
    ts = datetime.utcnow().isoformat() + "Z"
    summary = _excerpt_text(prompt_text, limit=500)

    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)

    stamp = str(int(time.time() * 1000))
    filename = f"pending_prompt_{session_id}_{stamp}.json"
    metadata: Dict[str, Any] = {
        "session": session_id,
        "session_id": session_id,
        "work_item": work_item_id,
        "summary": summary,
        "prompt_text": prompt_text,
        "choices": choices if choices is not None else [],
        "context": context if context is not None else [],
        "state": "waiting_for_input",
        "created_at": ts,
        "stamp": stamp,
    }
    path = os.path.join(out_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, sort_keys=True)
        LOG.info(
            "Wrote pending prompt for session=%s work_item=%s path=%s",
            session_id,
            work_item_id,
            path,
        )
    except Exception:
        LOG.exception("Failed to write pending prompt to %s", path)

    metadata["prompt_file"] = path
    metadata["pending_prompt_file"] = path
    metadata["tool_output_dir"] = out_dir

    set_session_state(session_id, "waiting_for_input")
    emit_internal_event("waiting_for_input", metadata)
    _send_waiting_for_input_notification(metadata)

    return metadata
