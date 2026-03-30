"""Thin conversation manager for resuming sessions waiting for human input.

This module provides a minimal, synchronous Python API used by other
components to start a conversation (record a pending prompt) and to resume
an existing session by supplying a human response.

The implementation is intentionally small and dependency-free: it reads and
writes the same tool-output files used by `session_block.py` and emits events
via the same helper so other processes can react.

OpenCode SDK integration is optional. Callers can pass an SDK client (for
example, a Python OpenCode SDK adapter) and this module will invoke its
`start_conversation` / `resume_session` hooks before updating local state.

Errors are raised for common failure modes so callers can act programmatically.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

try:
    # Prefer relative import when package-imported (ensures intra-package
    # imports resolve correctly when the package is installed under
    # .worklog/plugins/ampa_py/ampa). Fall back to top-level import for
    # script execution contexts.
    from . import session_block
except Exception:
    import session_block

LOG = logging.getLogger("conversation_manager")


class TimedOutError(Exception):
    """Raised when a pending prompt exceeded the configured resume timeout."""


class InvalidStateError(Exception):
    """Raised when the session is not in `waiting_for_input` state."""


class NotFoundError(Exception):
    """Raised when a session or pending prompt file cannot be located."""


class SDKError(Exception):
    """Raised when the optional OpenCode SDK integration fails."""


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    return os.path.join(tempfile.gettempdir(), "opencode_tool_output")


def start_conversation(
    session_id: str,
    prompt: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    sdk_client: Optional[Any] = None,
    choices: Optional[Any] = None,
    context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Record a pending prompt and set session state to `waiting_for_input`.

    This is a thin wrapper around `session_block.detect_and_surface_blocking_prompt`.
    """
    metadata = dict(metadata or {})
    sdk_client = sdk_client or metadata.pop("sdk_client", None)

    if sdk_client is not None:
        sdk_start = getattr(sdk_client, "start_conversation", None)
        if not callable(sdk_start):
            raise SDKError("sdk_client missing start_conversation")
        try:
            sdk_start(session_id, prompt, metadata)
        except Exception as exc:
            LOG.exception("OpenCode SDK start failed for session=%s", session_id)
            raise SDKError("OpenCode SDK start_conversation failed") from exc

    choices = metadata.get("choices") if choices is None else choices
    context = metadata.get("context") if context is None else context
    return session_block.detect_and_surface_blocking_prompt(
        session_id,
        metadata.get("work_item"),
        prompt,
        choices=choices,
        context=context,
    )


def _find_pending_prompt_file(
    session_id: str, tool_output_dir: Optional[str] = None
) -> Optional[str]:
    tool_output_dir = tool_output_dir or _tool_output_dir()
    pattern = os.path.join(tool_output_dir, f"pending_prompt_{session_id}_*.json")
    matches = glob.glob(pattern)
    if not matches:
        return None
    # return the latest by mtime
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def resume_session(
    session_id: str,
    response: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    timeout_seconds: Optional[int] = None,
    sdk_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Resume a session waiting for input by injecting `response`.

    Steps:
    - locate the most recent pending prompt file for `session_id`
    - verify `session_{session_id}.json` exists and has state `waiting_for_input`
    - verify the prompt hasn't timed out (default 24h)
    - append an event `resumed_with_input` to the events log and set session state to `running`

    Returns a dict with keys: `status`, `session`, `prompt_file`, `event_path`, `response`.
    Raises NotFoundError, InvalidStateError, or TimedOutError on failure.
    """
    metadata = dict(metadata or {})
    sdk_client = sdk_client or metadata.pop("sdk_client", None)
    tool_output_dir = metadata.get("tool_output_dir") or _tool_output_dir()

    prompt_file = _find_pending_prompt_file(session_id, tool_output_dir)
    if not prompt_file:
        LOG.warning("No pending prompt found for session=%s", session_id)
        raise NotFoundError(f"no pending prompt found for session={session_id}")

    try:
        with open(prompt_file, "r", encoding="utf-8") as fh:
            prompt_meta = json.load(fh)
    except Exception:
        LOG.exception("Failed reading pending prompt file=%s", prompt_file)
        raise NotFoundError(f"failed to read pending prompt file={prompt_file}")

    prompt_text = prompt_meta.get("prompt_text") or prompt_meta.get("summary") or ""
    choices = prompt_meta.get("choices")
    if choices is None:
        choices = []
    context = prompt_meta.get("context")
    if context is None:
        context = []

    # verify session state file
    session_path = os.path.join(tool_output_dir, f"session_{session_id}.json")
    if not os.path.exists(session_path):
        LOG.warning("Session state file missing for session=%s", session_id)
        raise NotFoundError(f"session state file not found: {session_path}")

    try:
        with open(session_path, "r", encoding="utf-8") as fh:
            session_state = json.load(fh)
    except Exception:
        LOG.exception("Failed reading session state file=%s", session_path)
        raise NotFoundError(f"unable to read session state file: {session_path}")

    state = session_state.get("state")
    if state != "waiting_for_input":
        LOG.warning("Invalid session state for session=%s state=%s", session_id, state)
        raise InvalidStateError(f"session={session_id} in invalid state={state}")

    # timeout check
    created_at = prompt_meta.get("created_at")
    if created_at:
        try:
            created_ts = datetime.fromisoformat(created_at.rstrip("Z"))
        except Exception:
            created_ts = None
    else:
        created_ts = None

    timeout_seconds = int(
        timeout_seconds or int(os.getenv("AMPA_RESUME_TIMEOUT_SECONDS", 24 * 3600))
    )
    if created_ts:
        if datetime.utcnow() - created_ts > timedelta(seconds=timeout_seconds):
            LOG.warning("Pending prompt timed out for session=%s", session_id)
            raise TimedOutError(f"pending prompt for session={session_id} timed out")

    resume_metadata = dict(metadata)
    resume_metadata.setdefault("prompt_text", prompt_text)
    resume_metadata.setdefault("choices", choices)
    resume_metadata.setdefault("context", context)

    if sdk_client is not None:
        sdk_resume = getattr(sdk_client, "resume_session", None)
        if not callable(sdk_resume):
            raise SDKError("sdk_client missing resume_session")
        try:
            sdk_resume(session_id, response, resume_metadata)
        except Exception as exc:
            LOG.exception("OpenCode SDK resume failed for session=%s", session_id)
            raise SDKError("OpenCode SDK resume_session failed") from exc

    # emit resumed event
    event_payload = {
        "session": session_id,
        "response": response,
        "prompt_file": prompt_file,
        "resumed_at": datetime.utcnow().isoformat() + "Z",
    }
    event_path = session_block.emit_internal_event("resumed_with_input", event_payload)

    # update session state to running
    session_block.set_session_state(session_id, "running")

    LOG.info(
        "Resumed session=%s prompt=%s response=%s", session_id, prompt_file, response
    )

    return {
        "status": "resumed",
        "session": session_id,
        "prompt_file": prompt_file,
        "prompt_text": prompt_text,
        "choices": choices,
        "context": context,
        "event_path": event_path,
        "response": response,
    }
