import json
import datetime as dt
from datetime import timezone

from ampa.intake_runner import IntakeRunner


class DummyProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class DummyStore:
    def __init__(self, initial=None):
        self._state = initial or {}

    def get_state(self, cid):
        return dict(self._state.get(cid, {}))

    def update_state(self, cid, state):
        # store a shallow copy to emulate persistence
        self._state[cid] = dict(state)


def make_proc_for_workitem(stage=None, status=None):
    wi = {"id": "WL-DET-1", "stage": stage, "status": status}
    payload = {"workItem": wi}
    return DummyProc(json.dumps(payload), 0)


def test_detects_intake_complete():
    # started a short time ago
    started = (dt.datetime.now(timezone.utc) - dt.timedelta(seconds=10)).isoformat()
    store = DummyStore({
        "intake-selector": {"intake_dispatches": {"WL-DET-1": {"started_at": started}}}
    })

    # run_shell returns wl show with stage=intake_complete
    def run_shell(cmd, **kwargs):
        return make_proc_for_workitem(stage="intake_complete")

    runner = IntakeRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = type("S", (), {"command_id": "intake-selector"})()

    runner._process_previous_dispatches(spec, store)

    state = store.get_state("intake-selector")
    assert "intake_metrics" in state
    m = state["intake_metrics"].get("WL-DET-1")
    assert m is not None
    assert m["outcome"] == "intake_complete"


def test_detects_input_needed():
    started = (dt.datetime.now(timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    store = DummyStore({
        "intake-selector": {"intake_dispatches": {"WL-DET-1": {"started_at": started}}}
    })

    def run_shell(cmd, **kwargs):
        return make_proc_for_workitem(status="input_needed")

    runner = IntakeRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = type("S", (), {"command_id": "intake-selector"})()

    runner._process_previous_dispatches(spec, store)

    state = store.get_state("intake-selector")
    assert "intake_metrics" in state
    m = state["intake_metrics"].get("WL-DET-1")
    assert m is not None
    assert m["outcome"] == "input_needed"


def test_timeout_marks_timeout(monkeypatch):
    # set a small timeout (1 second)
    monkeypatch.setenv("AMPA_INTAKE_COMPLETION_TIMEOUT", "1")

    started = (dt.datetime.now(timezone.utc) - dt.timedelta(seconds=2)).isoformat()
    store = DummyStore({
        "intake-selector": {"intake_dispatches": {"WL-DET-1": {"started_at": started}}}
    })

    # Ensure wl show is not even called by providing a run_shell that would
    # error if invoked; the code should mark as timeout without calling WL.
    def run_shell(cmd, **kwargs):
        raise AssertionError("wl show should not be called for timeout case")

    runner = IntakeRunner(run_shell=run_shell, command_cwd="/tmp")
    spec = type("S", (), {"command_id": "intake-selector"})()

    runner._process_previous_dispatches(spec, store)

    state = store.get_state("intake-selector")
    assert "intake_metrics" in state
    m = state["intake_metrics"].get("WL-DET-1")
    assert m is not None
    assert m["outcome"] == "timeout"
