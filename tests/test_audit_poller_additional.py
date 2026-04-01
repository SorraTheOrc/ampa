import datetime as dt
import json
import subprocess
from types import SimpleNamespace

from ampa import audit_poller


class _DummyStore:
    def __init__(self):
        self._states = {}

    def get_state(self, command_id: str):
        return dict(self._states.get(command_id, {}))

    def update_state(self, command_id: str, state: dict):
        # store a copy to mimic persistence
        self._states[command_id] = dict(state)


def _make_run_shell(stdout: str, returncode: int = 0, stderr: str = ""):
    def run_shell(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)

    return run_shell


def test_query_candidates_wrapper_shapes_and_key_ending_workitems():
    # Validate that wrapped responses under different keys are handled.
    base_item = {"id": "W-1", "title": "Wrapped", "updatedAt": "2020-01-01T00:00:00Z"}

    shapes = [
        json.dumps({"workItems": [base_item]}),
        json.dumps({"work_items": [base_item]}),
        json.dumps({"items": [base_item]}),
        json.dumps({"data": [base_item]}),
        json.dumps({"somethingWorkItems": [base_item]}),
    ]

    for s in shapes:
        run_shell = _make_run_shell(s)
        items = audit_poller._query_candidates(run_shell, cwd=".")
        assert items is not None
        assert any(it.get("id") == base_item["id"] for it in items)


def test_poll_and_handoff_returns_query_failed_on_invalid_json():
    # Invalid JSON should cause poll_and_handoff to return query_failed
    run_shell = _make_run_shell("{ this is not: valid json }")
    store = _DummyStore()
    spec = SimpleNamespace(command_id="cmd-json", metadata={})

    def handler(_):
        # should not be called
        raise AssertionError("handler should not be invoked on invalid json")

    res = audit_poller.poll_and_handoff(run_shell=run_shell, cwd=".", store=store, spec=spec, handler=handler)
    assert res.outcome == audit_poller.PollerOutcome.query_failed


def test_poll_and_handoff_returns_query_failed_on_nonzero_rc():
    # Non-zero return code should cause query failure
    run_shell = _make_run_shell("[]", returncode=2, stderr="boom")
    store = _DummyStore()
    spec = SimpleNamespace(command_id="cmd-rc", metadata={})

    def handler(_):
        raise AssertionError("handler should not be invoked on non-zero rc")

    res = audit_poller.poll_and_handoff(run_shell=run_shell, cwd=".", store=store, spec=spec, handler=handler)
    assert res.outcome == audit_poller.PollerOutcome.query_failed
