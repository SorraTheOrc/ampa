import datetime as dt
import json

from ampa.scheduler_types import (
    CommandSpec,
    RunResult,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore


def _deviation_ratio(observed_seconds, expected_seconds):
    if not observed_seconds or expected_seconds <= 0:
        return None
    observed_avg = sum(observed_seconds) / len(observed_seconds)
    return abs(observed_avg - expected_seconds) / expected_seconds


def _seed_last_runs(scheduler: Scheduler, now: dt.datetime) -> None:
    for spec in scheduler.store.list_commands():
        state = scheduler.store.get_state(spec.command_id)
        if not state.get("last_run_ts"):
            interval_seconds = spec.frequency_minutes * 60
            state["last_run_ts"] = (
                now - dt.timedelta(seconds=interval_seconds)
            ).isoformat()
            scheduler.store.update_state(spec.command_id, state)


def test_simulated_schedule_within_expected_deviation(tmp_path):
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    store_path = tmp_path / "scheduler_store.json"
    store_path.write_text(json.dumps({"commands": {}, "state": {}}))
    store = SchedulerStore(str(store_path))
    config = SchedulerConfig(
        poll_interval_seconds=10,
        global_min_interval_seconds=10,
        priority_weight=0.0,
        store_path=str(store_path),
        llm_healthcheck_url="http://localhost:8000/health",
        max_run_history=5,
    )

    def _executor(_spec: CommandSpec) -> RunResult:
        return RunResult(start_ts=now, end_ts=now, exit_code=0)

    scheduler = Scheduler(
        store,
        config,
        llm_probe=lambda _url: True,
        executor=_executor,
    )

    scheduler.store.add_command(CommandSpec("fast", "echo fast", False, 1, 0, {}))
    scheduler.store.add_command(CommandSpec("med", "echo med", False, 2, 0, {}))
    scheduler.store.add_command(CommandSpec("slow", "echo slow", False, 5, 0, {}))

    _seed_last_runs(scheduler, now)

    results = scheduler.simulate(duration_seconds=7200, tick_seconds=10, now=now)
    observed = results["observed"]
    assert observed["fast"]
    assert observed["med"]
    assert observed["slow"]

    for command_id, expected_minutes in (
        ("fast", 1),
        ("med", 2),
        ("slow", 5),
    ):
        expected = expected_minutes * 60
        deviation = _deviation_ratio(observed[command_id], expected)
        assert deviation is not None
        assert deviation <= 0.2
