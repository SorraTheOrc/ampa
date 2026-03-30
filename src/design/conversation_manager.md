Conversation manager design — API & OpenCode SDK notes

Overview
- Purpose: define a minimal Python callable conversation manager to resume sessions waiting for human input. This doc prepares implementers to use the official OpenCode Python SDK (`opencode-ai`) as the integration surface.

API surface (Python callables)
- File: `ampa/conversation_manager.py`
- Functions (sync API v1):
  - `def start_conversation(session_id: str, prompt: str, metadata: dict) -> dict` — create/init a conversation context and return conversation metadata.
  - `def resume_session(session_id: str, response: str, metadata: dict) -> dict` — validate pending prompt, inject response into the running session, and return result/next-state.

Errors
- `class TimedOutError(Exception)`: raised when a pending prompt exceeded configured resume timeout.
- `class InvalidStateError(Exception)`: raised when session is not in `waiting_for_input`.
- `class NotFoundError(Exception)`: raised when session or pending prompt file cannot be located.

File interactions
- `session_block.py` writes pending prompt files named `pending_prompt_<session_id>_<stamp>.json` and `session_<session_id>.json`. The manager MUST read these files (tool-output dir configured via `AMPA_TOOL_OUTPUT_DIR`) to locate prompt context and verify `state == "waiting_for_input"`.
- On resume success the manager appends an event to `events.jsonl` and may update `session_<session_id>.json` to reflect the resumed state.

Pending prompt payload (JSON)
- Stored under `AMPA_TOOL_OUTPUT_DIR` as `pending_prompt_<session_id>_<stamp>.json`.
- Includes full prompt text, available choices, and conversation context.

Example:
```
{
  "session": "s-123",
  "session_id": "s-123",
  "work_item": "WL-1",
  "summary": "Please confirm the change",
  "prompt_text": "Please confirm the change",
  "choices": ["yes", "no"],
  "context": [{"role": "user", "content": "ship it"}],
  "state": "waiting_for_input",
  "created_at": "2026-02-11T12:00:00Z",
  "stamp": "1739275200000"
}
```

OpenCode SDK usage notes
- Dependency: `opencode-ai` (PyPI package name). Install/pin via `pip install --pre opencode-ai` (pin exact version in the implementation's requirements or `pyproject.toml`/`requirements.txt`).
- Import examples:
  - sync client: `from opencode_ai import Opencode`
  - async client: `from opencode_ai import AsyncOpencode`

SDK integration guidance
- For v1 keep SDK usage minimal: use `Opencode` to fetch session metadata or to post events if required. Avoid complex streaming features in v1.
- Example (sync) usage snippet:
```
from opencode_ai import Opencode

client = Opencode()
session = client.session.get(session_id)
```

Readiness checklist
 - [ ] Add `opencode-ai` to project dependencies (pin version).
 - [ ] Implement `ampa/conversation_manager.py` with callable API and error classes.
 - [ ] Unit tests: `tests/test_conversation_manager.py`.
 - [ ] Integration test: simulate `waiting_for_input` and call `ampa/responder.py` to resume.
 - [ ] README snippet showing the import and basic call sequence.

Acceptance criteria for design task
 - API signatures documented (above).
 - SDK import example present.
 - Checklist items listed and actionable.
