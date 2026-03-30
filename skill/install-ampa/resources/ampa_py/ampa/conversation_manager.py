from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

try:
    from . import session_block
except Exception:
    import session_block

LOG = logging.getLogger("conversation_manager")


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    return os.path.join(tempfile.gettempdir(), "opencode_tool_output")


def start_conversation(
    session_id: str, prompt: str, metadata: Optional[Dict[str, Any]] = None, **kwargs
) -> Dict[str, Any]:
    metadata = dict(metadata or {})
    choices = metadata.get("choices")
    context = metadata.get("context")
    return session_block.detect_and_surface_blocking_prompt(
        session_id, metadata.get("work_item"), prompt, choices=choices, context=context
    )
