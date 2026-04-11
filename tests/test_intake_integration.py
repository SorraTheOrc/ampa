import json
import datetime as dt

from ampa.intake_runner import IntakeRunner
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_helpers import ensure_intake_command


class DummyStore(SchedulerStore):
    def __init__(self):
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def test_ensure_intake_skips_in_memory():
    store = DummyStore()
    # should not raise and should not add command
    ensure_intake_command(store)
    assert store.list_commands() == []


def test_intake_runner_persists_selection(tmp_path):
    # runner returns a wrapped list under workItems
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {"workItems": [{"id": "W1", "sortIndex": 5, "updatedAt": now}]}

    class DummyProc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_shell(cmd, **kwargs):
        return DummyProc(json.dumps(payload), 0)

    # simple store backed by file
    path = str(tmp_path / "store.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"commands": {}, "state": {}, "last_global_start_ts": None}))

    store = SchedulerStore(path)
    # add a fake command spec id for state persistence
    spec = type("S", (), {"command_id": "intake-selector"})

    runner = IntakeRunner(run_shell=run_shell, command_cwd=str(tmp_path))
    res = runner.run(spec, store)
    assert res.get("selected") == "W1"
    state = store.get_state("intake-selector")
    assert "last_selected_at_by_item" in state
    assert "W1" in state["last_selected_at_by_item"]
