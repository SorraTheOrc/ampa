import datetime as dt
import json
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


class DummyProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


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


@pytest.mark.parametrize(
    "payload",
    [
        {"workItem": {"id": "WL-DET-1", "stage": "plan_complete"}},
        {"workItems": [{"id": "WL-DET-1", "stage": "plan_complete"}]},
        {"id": "WL-DET-1", "stage": "plan_complete"},
    ],
)
def test_process_previous_dispatches_detects_plan_complete_across_payload_shapes(payload):
    from ampa.plan_runner import PlanRunner

    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)).isoformat()
    store = make_store()
    store.update_state(
        "plan-runner",
        {"plan_dispatches": {"WL-DET-1": {"started_at": started, "pid": 12345}}},
    )

    def run_shell(cmd, **kwargs):
        return DummyProc(json.dumps(payload), 0)

    runner = PlanRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = SimpleNamespace(command_id="plan-runner", metadata={})

    runner._process_previous_dispatches(spec, store)

    state = store.get_state("plan-runner")
    metric = state["plan_metrics"]["WL-DET-1"]
    assert metric["outcome"] == "plan_complete"
    assert metric["duration_seconds"] >= 0

    dispatch_entry = state["plan_dispatches"]["WL-DET-1"]
    assert dispatch_entry["observed"] is True
    assert "pid" not in dispatch_entry


def test_process_previous_dispatches_marks_timeout_and_clears_pid(monkeypatch):
    from ampa.plan_runner import PlanRunner

    monkeypatch.setenv("AMPA_PLAN_COMPLETION_TIMEOUT", "1")

    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)).isoformat()
    store = make_store()
    store.update_state(
        "plan-runner",
        {"plan_dispatches": {"WL-DET-1": {"started_at": started, "pid": 9999}}},
    )

    def run_shell(cmd, **kwargs):
        raise AssertionError("wl show should not be called for timeout case")

    runner = PlanRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = SimpleNamespace(command_id="plan-runner", metadata={})

    runner._process_previous_dispatches(spec, store)

    state = store.get_state("plan-runner")
    metric = state["plan_metrics"]["WL-DET-1"]
    assert metric["outcome"] == "timeout"

    dispatch_entry = state["plan_dispatches"]["WL-DET-1"]
    assert dispatch_entry["observed"] is True
    assert "pid" not in dispatch_entry


def test_run_processes_previous_dispatches_before_query(monkeypatch):
    from ampa.plan_runner import PlanRunner
    import ampa.plan_runner as pr

    store = make_store()
    spec = SimpleNamespace(command_id="plan-runner", metadata={})
    call_order = []

    class DummySelector:
        def __init__(self, run_shell=None, cwd=None):
            pass

        def query_candidates(self):
            call_order.append("query")
            return []

    monkeypatch.setattr(pr, "PlanCandidateSelector", DummySelector)

    runner = PlanRunner(run_shell=lambda *a, **k: None, command_cwd=".")

    def fake_process_previous_dispatches(_spec, _store):
        call_order.append("process")

    monkeypatch.setattr(runner, "_process_previous_dispatches", fake_process_previous_dispatches)

    result = runner.run(spec, store)

    assert result == {"planned": None}
    assert call_order == ["process", "query"]


def test_process_previous_dispatches_updates_plan_prometheus_metrics(monkeypatch):
    import ampa.server as srv
    from ampa.plan_runner import PlanRunner

    # Reset internal delta tracker so this test can assert increments deterministically.
    monkeypatch.setattr(srv, "_last_plan_dispatched_total", 0)

    now = dt.datetime.now(dt.timezone.utc)
    started_1 = (now - dt.timedelta(seconds=10)).isoformat()
    started_2 = (now - dt.timedelta(seconds=30)).isoformat()

    store = make_store()
    store.update_state(
        "plan-runner",
        {
            "plan_dispatches": {
                "WL-MET-1": {"started_at": started_1, "pid": 111},
                "WL-MET-2": {"started_at": started_2, "pid": 222},
            }
        },
    )

    # First item is complete, second item times out.
    monkeypatch.setenv("AMPA_PLAN_COMPLETION_TIMEOUT", "20")

    def run_shell(cmd, **kwargs):
        if "WL-MET-1" in cmd:
            return DummyProc(json.dumps({"workItem": {"id": "WL-MET-1", "stage": "plan_complete"}}), 0)
        return DummyProc(json.dumps({"workItem": {"id": "WL-MET-2", "stage": "intake_complete"}}), 0)

    before_counter = srv.ampa_plan_dispatched_total._value.get()

    runner = PlanRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = SimpleNamespace(command_id="plan-runner", metadata={})
    runner._process_previous_dispatches(spec, store)

    state = store.get_state("plan-runner")
    metrics = state["plan_metrics"]
    total = len(metrics)
    successes = sum(1 for m in metrics.values() if m.get("outcome") == "plan_complete")
    total_duration = sum(int(m.get("duration_seconds", 0)) for m in metrics.values())

    assert total == 2
    assert metrics["WL-MET-1"]["outcome"] == "plan_complete"
    assert metrics["WL-MET-2"]["outcome"] == "timeout"

    # Counter increments only by observed total delta.
    assert srv.ampa_plan_dispatched_total._value.get() == pytest.approx(before_counter + total)
    assert srv.ampa_plan_success_rate._value.get() == pytest.approx(successes / total)
    assert srv.ampa_plan_avg_completion_seconds._value.get() == pytest.approx(total_duration / total)


def test_process_previous_dispatches_counter_delta_prevents_double_count(monkeypatch):
    import ampa.server as srv
    from ampa.plan_runner import PlanRunner

    monkeypatch.setattr(srv, "_last_plan_dispatched_total", 0)

    store = make_store()
    started_1 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    store.update_state(
        "plan-runner",
        {"plan_dispatches": {"WL-DELTA-1": {"started_at": started_1, "pid": 10}}},
    )

    def run_shell(cmd, **kwargs):
        return DummyProc(json.dumps({"workItem": {"stage": "plan_complete"}}), 0)

    runner = PlanRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = SimpleNamespace(command_id="plan-runner", metadata={})

    base = srv.ampa_plan_dispatched_total._value.get()
    runner._process_previous_dispatches(spec, store)
    after_first = srv.ampa_plan_dispatched_total._value.get()

    # Re-processing without new observations must not inflate the counter.
    runner._process_previous_dispatches(spec, store)
    after_second = srv.ampa_plan_dispatched_total._value.get()

    # Add one new dispatch outcome and verify only +1 is added.
    state = store.get_state("plan-runner")
    state.setdefault("plan_dispatches", {})["WL-DELTA-2"] = {
        "started_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat(),
        "pid": 11,
    }
    store.update_state("plan-runner", state)

    runner._process_previous_dispatches(spec, store)
    after_third = srv.ampa_plan_dispatched_total._value.get()

    assert after_first == pytest.approx(base + 1)
    assert after_second == pytest.approx(after_first)
    assert after_third == pytest.approx(after_second + 1)
