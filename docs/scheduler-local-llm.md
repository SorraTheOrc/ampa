# Scheduler Local LLM Agent Mapping

This document describes how AMPA scheduler commands can choose an LLM endpoint
by agent name.

## Command descriptor field

Commands can include an `agent` field:

```json
{
  "id": "wl-audit",
  "command": "true",
  "requires_llm": true,
  "agent": "Casey",
  "frequency_minutes": 2,
  "priority": 0,
  "metadata": {},
  "type": "audit"
}
```

If `agent` is omitted and `requires_llm` is `true`, AMPA uses the default
agent name `Casey`.

## Agent endpoint mapping

Configure agent-to-endpoint mapping with environment variables:

```bash
AMPA_DEFAULT_LLM_AGENT="Casey"
AMPA_LLM_HEALTHCHECK_URL="http://localhost:8000/health"
AMPA_LLM_AGENT_ENDPOINTS='{"Casey":"http://localhost:8000/health","Riley":"http://localhost:8100/health"}'
```

Resolution rules:

1. Use command `agent` when provided.
2. Otherwise use `AMPA_DEFAULT_LLM_AGENT` (defaults to `Casey`).
3. Resolve endpoint from `AMPA_LLM_AGENT_ENDPOINTS`.
4. Fallback to `AMPA_LLM_HEALTHCHECK_URL` if no mapping entry exists.

## Verification checklist

1. Run focused scheduler tests:
   - `pytest -q tests/test_scheduler_scoring.py tests/test_scheduler_types_from_iso.py`
2. Verify list formatting tests (agent field visible in detail output):
   - `pytest -q tests/test_scheduler_run.py tests/test_scheduler_list.py`
3. Run full suite:
   - `pytest -q`
