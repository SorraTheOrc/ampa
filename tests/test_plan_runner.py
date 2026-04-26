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


def test_plan_success_clears_retries(monkeypatch):
    from ampa.plan_runner import PlanRunner

    store = make_store()
    cmd_id = "plan-runner"
    # seed existing retry state for an item
    state = {"plan_retries": {"WL-1": {"attempts": 2}}}
    store.update_state(cmd_id, state)

    # fake dispatcher that returns a successful DispatchResult
    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=True, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=123)

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.plan_runner as pr
    # patch plan candidate selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(pr, "PlanCandidateSelector", DummySelector)

    monkeypatch.setattr(pr, "PlanDispatcher", lambda *a, **k: DummyDispatcher())

    runner = PlanRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    spec = SimpleNamespace(command_id=cmd_id, metadata={})

    # also patch notifications.notify to ensure it's not called
    called = {}

    def fake_notify(*a, **k):
        called['notified'] = True

    monkeypatch.setattr(pr, "notifications", SimpleNamespace(notify=fake_notify))

    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    # retries for WL-1 should be cleared on success
    assert st.get("plan_retries", {}) == {}
    assert res["planned"] == "WL-1"


def test_plan_failure_schedules_backoff(monkeypatch):
    from ampa.plan_runner import PlanRunner

    store = make_store()
    cmd_id = "plan-runner"
    store.update_state(cmd_id, {})

    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=False, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=None, error="boom")

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.plan_runner as pr
    # patch plan candidate selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(pr, "PlanCandidateSelector", DummySelector)
    monkeypatch.setattr(pr, "PlanDispatcher", lambda *a, **k: DummyDispatcher())

    notified = {}

    def fake_notify(*a, **k):
        notified.setdefault("calls", []).append((a, k))

    monkeypatch.setattr(pr, "notifications", SimpleNamespace(notify=fake_notify))

    runner = PlanRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    spec = SimpleNamespace(command_id=cmd_id, metadata={"backoff_base_minutes": 15, "max_retries": 3})

    before = dt.datetime.now(dt.timezone.utc)
    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    retries = st.get("plan_retries", {})
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


def test_plan_posts_worklog_comment_on_dispatch(monkeypatch):
    from ampa.plan_runner import PlanRunner

    store = make_store()
    cmd_id = "plan-runner"
    store.update_state(cmd_id, {})

    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            timestamp=dt.datetime.now(dt.timezone.utc),
            pid=456,
        )

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.plan_runner as pr

    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(pr, "PlanCandidateSelector", DummySelector)
    monkeypatch.setattr(pr, "PlanDispatcher", lambda *a, **k: DummyDispatcher())

    calls = []

    class DummyProc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def run_shell(cmd, **kwargs):
        calls.append(cmd)
        return DummyProc()

    runner = PlanRunner(run_shell=run_shell, command_cwd=".")
    spec = SimpleNamespace(command_id=cmd_id, metadata={})

    res = runner.run(spec, store)
    assert res["planned"] == "WL-1"

    comment_calls = [c for c in calls if c.startswith("wl comment add WL-1")]
    assert comment_calls, "Expected a wl comment add call for dispatched plan"
    assert '--author "ampa"' in comment_calls[0]
    assert "Automated plan dispatched by AMPA" in comment_calls[0]


def test_plan_permanent_failure_notifies(monkeypatch):
    from ampa.plan_runner import PlanRunner

    store = make_store()
    cmd_id = "plan-runner"
    store.update_state(cmd_id, {})

    from ampa.engine.dispatch import DispatchResult

    def fake_dispatch(command, work_item_id):
        return DispatchResult(success=False, command=command, work_item_id=work_item_id, timestamp=dt.datetime.now(dt.timezone.utc), pid=None, error="boom")

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return fake_dispatch(command, work_item_id)

    import ampa.plan_runner as pr
    # patch plan candidate selector to return a deterministic candidate
    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            return [{"id": "WL-1", "title": "Test Item"}]

        def select_top(self, candidates):
            return candidates[0] if candidates else None

    monkeypatch.setattr(pr, "PlanCandidateSelector", DummySelector)
    monkeypatch.setattr(pr, "PlanDispatcher", lambda *a, **k: DummyDispatcher())

    calls = []

    def fake_notify(title, body, message_type=None):
        calls.append((title, body, message_type))

    monkeypatch.setattr(pr, "notifications", SimpleNamespace(notify=fake_notify))

    runner = PlanRunner(run_shell=lambda *a, **k: None, command_cwd=".")
    # set max_retries to 1 so first failure becomes permanent
    spec = SimpleNamespace(command_id=cmd_id, metadata={"max_retries": 1})

    res = runner.run(spec, store)
    st = store.get_state(cmd_id)
    retries = st.get("plan_retries", {})
    entry = retries.get("WL-1")
    assert entry is not None
    assert entry.get("permanent_failure") is True
    # Notification should have been sent
    assert any("permanent failure" in (t.lower() if t else "") for t, *_ in calls)