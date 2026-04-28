import json
import datetime as dt
from types import SimpleNamespace

from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_types import CommandSpec


class DummyProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_plan_runner_integration_processes_previous_and_dispatches(tmp_path, monkeypatch):
    """Integration-style test: verifies previous dispatches are observed and a new
    dispatch is recorded and commented on.

    - Registers a plan-runner CommandSpec in a file-backed SchedulerStore
    - Seeds plan_dispatches with a previous dispatch for WL-PR-1
    - Mocks wl show to report the previous item as plan_complete
    - Mocks wl next to return one intake_complete candidate WL-NEW-1
    - Mocks PlanDispatcher to return a successful DispatchResult for the new dispatch
    - Captures wl comment invocations to assert a Worklog comment was posted
    - Asserts plan_metrics updated for the previous dispatch and plan_dispatches contains the new dispatch
    """
    # prepare file-backed SchedulerStore
    store_path = str(tmp_path / "store.json")
    with open(store_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"commands": {}, "state": {}, "last_global_start_ts": None}))

    store = SchedulerStore(store_path)

    # add a fake command spec for the plan-runner
    spec = CommandSpec(
        command_id="plan-runner",
        command="run-plan",
        requires_llm=False,
        frequency_minutes=15,
        priority=5,
        metadata={"enabled": True},
        title="Plan Runner",
    )
    store.add_command(spec)

    # seed previous dispatch state for WL-PR-1
    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)).isoformat()
    initial_state = {"plan_dispatches": {"WL-PR-1": {"started_at": started, "pid": 111}}, "plan_retries": {}}
    store.update_state("plan-runner", initial_state)

    # capture run_shell calls
    calls = []

    def run_shell(cmd, **kwargs):
        calls.append(cmd)
        # wl show for previous dispatch -> report plan_complete
        if cmd.startswith("wl show WL-PR-1"):
            return DummyProc(json.dumps({"workItem": {"id": "WL-PR-1", "stage": "plan_complete"}}), 0)
        # wl next for intake_complete -> return candidate
        if cmd.startswith("wl next --stage intake_complete"):
            return DummyProc(json.dumps([{"id": "WL-NEW-1", "title": "New Item"}]), 0)
        # wl comment add -> return success
        if cmd.startswith("wl comment add"):
            return DummyProc(json.dumps({}), 0)
        # fallback
        return DummyProc(json.dumps({}), 0)

    # monkeypatch PlanDispatcher to a dummy that returns a successful DispatchResult
    from ampa.engine.dispatch import DispatchResult

    class DummyDispatcher:
        def dispatch(self, command, work_item_id):
            return DispatchResult(
                success=True,
                command=command,
                work_item_id=work_item_id,
                timestamp=dt.datetime.now(dt.timezone.utc),
                pid=222,
            )

    import ampa.plan_runner as pr

    monkeypatch.setattr(pr, "PlanDispatcher", lambda *a, **k: DummyDispatcher())

    runner = pr.PlanRunner(run_shell=run_shell, command_cwd=str(tmp_path))
    runner_spec = SimpleNamespace(command_id="plan-runner", metadata={})

    res = runner.run(runner_spec, store)

    # Validate returned result indicates the new item was planned
    assert res.get("planned") == "WL-NEW-1"

    st = store.get_state("plan-runner")
    # previous dispatch should have been observed and recorded in plan_metrics
    assert "plan_metrics" in st
    assert "WL-PR-1" in st["plan_metrics"]
    assert st["plan_metrics"]["WL-PR-1"]["outcome"] == "plan_complete"

    # previous dispatch entry should be marked observed and have pid cleared
    assert st["plan_dispatches"]["WL-PR-1"].get("observed") is True
    assert "pid" not in st["plan_dispatches"]["WL-PR-1"]

    # new dispatch record should be present
    assert "WL-NEW-1" in st["plan_dispatches"]
    new_entry = st["plan_dispatches"]["WL-NEW-1"]
    assert new_entry.get("pid") == 222 or isinstance(new_entry.get("started_at"), str)

    # ensure a wl comment add was invoked for the new dispatch and used author "ampa"
    comment_calls = [c for c in calls if c.startswith("wl comment add")]
    assert any("WL-NEW-1" in c and "--author \"ampa\"" in c for c in comment_calls)

    # spec should be registered with the scheduler store and have frequency 15
    got = store.get_command("plan-runner")
    assert got is not None
    assert got.frequency_minutes == 15
