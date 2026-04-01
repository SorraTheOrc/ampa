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


def _make_run_shell_with_items(items):
    # Return a run_shell-like callable that returns a CompletedProcess
    def run_shell(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(items), stderr="")

    return run_shell


def test_from_iso_z_terminator():
    t = audit_poller._from_iso("2023-01-02T03:04:05Z")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_plus_offset():
    t = audit_poller._from_iso("2023-01-02T03:04:05+00:00")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_naive_assumed_utc():
    t = audit_poller._from_iso("2023-01-02T03:04:05")
    assert t is not None
    # Naive timestamps should be coerced to UTC
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_handler_returns_false_does_not_update_last_audit():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    store = _DummyStore()
    spec = SimpleNamespace(command_id="cmd-test", metadata={})

    item = {"id": "I-1", "title": "T", "updatedAt": "2023-01-01T00:00:00Z"}
    run_shell = _make_run_shell_with_items([item])

    def handler_fail(work_item: dict) -> bool:
        return False

    res = audit_poller.poll_and_handoff(
        run_shell=run_shell,
        cwd=".",
        store=store,
        spec=spec,
        handler=handler_fail,
        now=now,
    )

    assert res.outcome == audit_poller.PollerOutcome.handed_off
    state = store.get_state(spec.command_id)
    # last_audit_at_by_item must not be updated on failure
    assert state.get("last_audit_at_by_item", {}).get(item["id"]) is None
    # last_attempt_at_by_item should be recorded
    assert state.get("last_attempt_at_by_item", {}).get(item["id"]) == now.isoformat()


def test_handler_raises_and_does_not_update_last_audit():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    store = _DummyStore()
    spec = SimpleNamespace(command_id="cmd-test", metadata={})

    item = {"id": "I-2", "title": "T2", "updatedAt": "2023-01-01T00:00:00Z"}
    run_shell = _make_run_shell_with_items([item])

    def handler_raises(work_item: dict) -> bool:
        raise RuntimeError("boom")

    res = audit_poller.poll_and_handoff(
        run_shell=run_shell,
        cwd=".",
        store=store,
        spec=spec,
        handler=handler_raises,
        now=now,
    )

    assert res.outcome == audit_poller.PollerOutcome.handed_off
    state = store.get_state(spec.command_id)
    # last_audit_at_by_item must not be updated on exception
    assert state.get("last_audit_at_by_item", {}).get(item["id"]) is None
    # last_attempt_at_by_item should be recorded
    assert state.get("last_attempt_at_by_item", {}).get(item["id"]) == now.isoformat()


def test_handler_success_updates_last_audit():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    store = _DummyStore()
    spec = SimpleNamespace(command_id="cmd-test", metadata={})

    item = {"id": "I-3", "title": "T3", "updatedAt": "2023-01-01T00:00:00Z"}
    run_shell = _make_run_shell_with_items([item])

    def handler_ok(work_item: dict) -> bool:
        return True

    res = audit_poller.poll_and_handoff(
        run_shell=run_shell,
        cwd=".",
        store=store,
        spec=spec,
        handler=handler_ok,
        now=now,
    )

    assert res.outcome == audit_poller.PollerOutcome.handed_off
    state = store.get_state(spec.command_id)
    # last_audit_at_by_item should be set to now
    assert state.get("last_audit_at_by_item", {}).get(item["id"]) == now.isoformat()
    # last_attempt_at_by_item should also be recorded
    assert state.get("last_attempt_at_by_item", {}).get(item["id"]) == now.isoformat()
