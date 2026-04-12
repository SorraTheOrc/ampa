import datetime as dt
from types import SimpleNamespace

import pytest


def make_store():
    """Simple in-memory fake store implementing required API."""
    data = {}

    class FakeStore:
        def get_state(self, command_id: str):
            return dict(data.get(command_id, {}))

        def update_state(self, command_id: str, state: dict):
            # store a shallow copy
            data[command_id] = dict(state)

    return FakeStore()


def test_intake_success_clears_retries(monkeypatch):
    from ampa.intake_runner import IntakeRunner

    store = make_store()
    cmd_id = "intake-selector"
    # seed existing retry state for an item
    state = {"intake_retries": {"WL-1": {"attempts": 2}}}
    store.update_state(cmd_id, state)

    # fake dispatcher that returns a successful DispatchResult
    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=True, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=123)

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    # patch IntakeDispatcher to use our dummy
    import ampa.intake_runner as ir
    # patch intake selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(ir, "IntakeCandidateSelector", DummySelector)

    monkeypatch.setattr(ir, "IntakeDispatcher", lambda: DummyDispatcher())

    runner = IntakeRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    spec = SimpleNamespace(command_id=cmd_id, metadata={})

    # also patch notifications.notify to ensure it's not called
    called = {}

    def fake_notify(*a, **k):
        called['notified'] = True

    monkeypatch.setattr(ir, "notifications", SimpleNamespace(notify=fake_notify))

    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    # retries for WL-1 should be cleared on success
    assert st.get("intake_retries", {}) == {}
    assert res["selected"] == "WL-1"


def test_intake_failure_schedules_backoff(monkeypatch):
    from ampa.intake_runner import IntakeRunner

    store = make_store()
    cmd_id = "intake-selector"
    store.update_state(cmd_id, {})

    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=False, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=None, error="boom")

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.intake_runner as ir
    # patch intake selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(ir, "IntakeCandidateSelector", DummySelector)
    monkeypatch.setattr(ir, "IntakeDispatcher", lambda: DummyDispatcher())

    notified = {}

    def fake_notify(*a, **k):
        notified.setdefault("calls", []).append((a, k))

    monkeypatch.setattr(ir, "notifications", SimpleNamespace(notify=fake_notify))

    runner = IntakeRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    spec = SimpleNamespace(command_id=cmd_id, metadata={"backoff_base_minutes": 15, "max_retries": 3})

    before = dt.datetime.now(dt.timezone.utc)
    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    retries = st.get("intake_retries", {})
    assert "WL-1" in retries
    entry = retries["WL-1"]
    assert entry.get("attempts") == 1
    assert entry.get("permanent_failure") is False
    next_iso = entry.get("next_attempt")
    assert next_iso is not None
    next_dt = dt.datetime.fromisoformat(next_iso)
    # next attempt should be approximately now + 15 minutes
    delta = (next_dt - before).total_seconds()
    assert 14 * 60 <= delta <= 16 * 60


def test_intake_permanent_failure_notifies(monkeypatch):
    from ampa.intake_runner import IntakeRunner

    store = make_store()
    cmd_id = "intake-selector"
    store.update_state(cmd_id, {})

    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=False, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=None, error="boom")

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.intake_runner as ir
    # patch intake selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(ir, "IntakeCandidateSelector", DummySelector)
    monkeypatch.setattr(ir, "IntakeDispatcher", lambda: DummyDispatcher())

    calls = []

    def fake_notify(title, body, message_type=None):
        calls.append((title, body, message_type))

    monkeypatch.setattr(ir, "notifications", SimpleNamespace(notify=fake_notify))

    runner = IntakeRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    # set max_retries to 1 so first failure becomes permanent
    spec = SimpleNamespace(command_id=cmd_id, metadata={"max_retries": 1})

    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    retries = st.get("intake_retries", {})
    entry = retries.get("WL-1")
    assert entry is not None
    assert entry.get("permanent_failure") is True
    # Notification should have been sent
    assert any("permanent failure" in (t.lower() if t else "") for t, *_ in calls)
