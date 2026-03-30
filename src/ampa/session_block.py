# Utilities to detect and record blocking prompts for interactive sessions.
#
# This module provides a narrowly-scoped implementation used by the
# SA-0MLGALPM812GOPDC work-item: it marks a session as `waiting_for_input`,
# records a prompt summary and metadata to a tool-output directory, and emits a
# simple internal event (written to an events log) so other processes can react.
#
# The implementation is intentionally small and dependency-free so it can be
# integrated into existing code paths quickly. The location for persisted
# artifacts is taken from the environment variable `AMPA_TOOL_OUTPUT_DIR`; if
# unset a directory under the platform temporary directory is used.

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
    """Emit a simple internal event by appending a JSON line to events.log.

    Returns the path to the events log file.
    """
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
    """Record the session state to a small JSON file.

    Returns the path to the state file.
    """
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
) -> Dict[str, Any]:
    """Record that a prompt is blocking and surface minimal metadata.

    Behaviour:
    - set session state to `waiting_for_input` (writes a session JSON file)
    - write a pending prompt file under the tool-output dir with a short
      summary and metadata (session id, work-item id, timestamp)
    - emit an internal event `waiting_for_input` with the same metadata

    Returns the metadata dictionary written.
    """
    ts = datetime.utcnow().isoformat() + "Z"
    summary = _excerpt_text(prompt_text, limit=500)

    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)

    # filename uses timestamp to avoid races
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

    # Attempt to resolve a per-project fallback mode. If the resolved mode
    # indicates an automatic decision (auto-accept / auto-decline) apply it
    # immediately by resuming the session. Otherwise persist state, emit the
    # waiting event and notify humans.
    try:
        # Import locally to avoid any potential import cycles (conversation
        # manager imports this module). Fallback module is safe to import at
        # top-level but keep import here for locality and test isolation.
        try:
            from . import fallback as fallback_mod
        except Exception:
            import fallback as fallback_mod

        mode = fallback_mod.resolve_mode(work_item_id, tool_output_dir=out_dir)
    except Exception:
        LOG.exception("Failed to resolve fallback mode for session=%s", session_id)
        mode = "hold"

    # If configured for automatic handling (via env), attempt to apply the
    # decision now. Default is to NOT auto-apply so human notification still
    # occurs unless AMPA_AUTO_APPLY_FALLBACK is set to a truthy value.
    auto_apply = str(os.getenv("AMPA_AUTO_APPLY_FALLBACK", "")).strip().lower()
    auto_apply = auto_apply in ("1", "true", "yes")

    # If configured for automatic handling, attempt to apply the decision now
    if auto_apply and mode in ("auto-accept", "auto-decline"):
        # write waiting state so resume_session can validate state
        set_session_state(session_id, "waiting_for_input")
        response = "accept" if mode == "auto-accept" else "decline"
        try:
            # conversation_manager imports session_block; import lazily to
            # avoid circular import at module import time.
            try:
                from . import conversation_manager as conv
            except Exception:
                import conversation_manager as conv

            result = conv.resume_session(
                session_id, response, metadata={"tool_output_dir": out_dir}
            )
            LOG.info(
                "Auto-applied fallback mode=%s for session=%s response=%s",
                mode,
                session_id,
                response,
            )
            metadata["fallback_mode"] = mode
            metadata["fallback_applied"] = True
            metadata["fallback_response"] = response
            metadata["resume_result"] = result
            return metadata
        except Exception:
            LOG.exception(
                "Auto-fallback %s failed for session=%s; falling back to hold",
                mode,
                session_id,
            )

    # Otherwise persist waiting state and notify humans
    set_session_state(session_id, "waiting_for_input")
    emit_internal_event("waiting_for_input", metadata)
    _send_waiting_for_input_notification(metadata)

    return metadata
