import datetime as dt
from unittest import mock

from ampa.scheduler import Scheduler
from ampa.scheduler_cli import _build_command_listing
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_types import CommandRunResult, CommandSpec, SchedulerConfig


class DummyStore(SchedulerStore):
    def __init__(self):
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self):
        return None


def _make_config(store_path: str) -> SchedulerConfig:
    return SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=store_path,
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )


def _make_run_result() -> CommandRunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return CommandRunResult(start_ts=start, end_ts=end, exit_code=0, output="ok")


def test_scheduler_auto_registers_plan_runner_and_lists_it(tmp_path):
    store_path = tmp_path / "scheduler_store.json"
    store_path.write_text('{"commands": {}, "state": {}, "last_global_start_ts": null}')
    store = SchedulerStore(str(store_path))

    with mock.patch("ampa.scheduler.build_engine", return_value=(None, None)):
        Scheduler(
            store=store,
            config=_make_config(str(store_path)),
            executor=lambda _spec: _make_run_result(),
            run_shell=lambda *a, **k: None,
        )

    plan_spec = store.get_command("plan-runner")
    assert plan_spec is not None
    assert plan_spec.command_type == "plan-runner"
    assert plan_spec.frequency_minutes == 15

    rows = _build_command_listing(store)
    assert any(row["id"] == "plan-runner" for row in rows)


def test_scheduler_start_command_routes_plan_runner_when_enabled(monkeypatch):
    store = DummyStore()
    spec = CommandSpec(
        command_id="plan-runner",
        command="echo plan-runner",
        requires_llm=False,
        frequency_minutes=15,
        priority=5,
        metadata={"enabled": True},
        title="Plan Runner",
        command_type="plan-runner",
    )
    store.add_command(spec)

    with mock.patch("ampa.scheduler.build_engine", return_value=(None, None)):
        sched = Scheduler(
            store=store,
            config=_make_config(":memory:"),
            executor=lambda _spec: _make_run_result(),
            run_shell=lambda *a, **k: None,
        )

    called = {}

    class DummyPlanRunner:
        def __init__(self, run_shell, command_cwd):
            called["init"] = (run_shell, command_cwd)

        def run(self, spec_arg, store_arg):
            called["run"] = (spec_arg.command_id, store_arg)
            return {"planned": "WL-123", "dispatch": True}

    import ampa.plan_runner as pr

    monkeypatch.setattr(pr, "PlanRunner", DummyPlanRunner)

    result = sched.start_command(spec)
    assert result.exit_code == 0
    assert called.get("run") is not None
    assert called["run"][0] == "plan-runner"


def test_scheduler_start_command_skips_plan_runner_when_disabled(monkeypatch):
    store = DummyStore()
    spec = CommandSpec(
        command_id="plan-runner",
        command="echo plan-runner",
        requires_llm=False,
        frequency_minutes=15,
        priority=5,
        metadata={"enabled": False},
        title="Plan Runner",
        command_type="plan-runner",
    )
    store.add_command(spec)

    with mock.patch("ampa.scheduler.build_engine", return_value=(None, None)):
        sched = Scheduler(
            store=store,
            config=_make_config(":memory:"),
            executor=lambda _spec: _make_run_result(),
            run_shell=lambda *a, **k: None,
        )

    class DummyPlanRunner:
        def __init__(self, run_shell, command_cwd):
            raise AssertionError("PlanRunner should not be constructed when disabled")

    import ampa.plan_runner as pr

    monkeypatch.setattr(pr, "PlanRunner", DummyPlanRunner)

    result = sched.start_command(spec)
    assert result.exit_code == 0
