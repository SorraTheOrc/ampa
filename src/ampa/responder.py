"""Responder adapter for resuming sessions awaiting human input.

This module provides a minimal callable that maps responder payloads into the
conversation manager API. It is intentionally small and dependency-free so it
can be used by CLI scripts, HTTP handlers, or tests.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import conversation_manager


def resume_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a waiting session using a responder payload.

    Expected payload keys:
    - session_id or session
    - response or input
    - metadata (optional dict)
    - action (optional: accept|decline|respond)
    - timeout_seconds (optional int)
    - sdk_client (optional OpenCode SDK adapter)
    """
    session_id = payload.get("session_id") or payload.get("session")
    response = payload.get("response") or payload.get("input")
    if not session_id:
        raise ValueError("payload missing session_id")

    action = payload.get("action")
    if response is None:
        if action is None:
            raise ValueError("payload missing response")
        action_text = str(action).strip().lower()
        if action_text in ("accept", "auto-accept", "auto_accept"):
            response = "accept"
        elif action_text in ("decline", "auto-decline", "auto_decline"):
            response = "decline"
        elif action_text in ("respond", "response"):
            raise ValueError("payload missing response")
        else:
            raise ValueError("payload action must be accept, decline, or respond")

    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("payload metadata must be a dict")

    timeout_seconds: Optional[int] = payload.get("timeout_seconds")
    sdk_client = payload.get("sdk_client")

    return conversation_manager.resume_session(
        session_id,
        str(response),
        metadata,
        timeout_seconds=timeout_seconds,
        sdk_client=sdk_client,
    )
