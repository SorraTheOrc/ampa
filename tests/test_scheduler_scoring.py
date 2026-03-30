import datetime as dt
import json
import os

from ampa.scheduler_types import (
    CommandSpec,
    RunResult,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def _make_scheduler(now: dt.datetime, llm_available: bool) -> Scheduler:
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=30,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost:8000/health",
        max_run_history=5,
    )

    def _executor(_spec: CommandSpec) -> RunResult:
        return RunResult(start_ts=now, end_ts=now, exit_code=0)

    scheduler = Scheduler(
        store,
        config,
        llm_probe=lambda _url: llm_available,
        executor=_executor,
    )
    # The watchdog command is auto-registered at init.  Give it a recent
    # last_run_ts so it doesn't interfere with scoring tests that care
    # about specific command ordering.
    scheduler.store.update_state(
        "stale-delegation-watchdog",
        {"last_run_ts": now.isoformat()},
    )
    # Exclude removed test-button command
    # Same for the auto-delegate command (auto-registered at init,
    # disabled by default).
    scheduler.store.update_state(
        "auto-delegate",
        {"last_run_ts": now.isoformat()},
    )
    # Same for the pr-monitor command (auto-registered at init).
    scheduler.store.update_state(
        "pr-monitor",
        {"last_run_ts": now.isoformat()},
    )
    return scheduler


def test_never_run_command_beats_recently_run():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    scheduler = _make_scheduler(now, llm_available=True)
    cmd_a = CommandSpec("a", "echo a", False, 10, 0, {})
    cmd_b = CommandSpec("b", "echo b", False, 10, 0, {})
    scheduler.store.add_command(cmd_a)
    scheduler.store.add_command(cmd_b)
    scheduler.store.update_state(
        "b", {"last_run_ts": (now - dt.timedelta(minutes=1)).isoformat()}
    )
    selected = scheduler.select_next(now)
    assert selected is not None
    assert selected.command_id == "a"


def test_high_frequency_command_scores_when_overdue():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    scheduler = _make_scheduler(now, llm_available=True)
    fast = CommandSpec("fast", "echo fast", False, 1, 0, {})
    slow = CommandSpec("slow", "echo slow", False, 10, 0, {})
    scheduler.store.add_command(fast)
    scheduler.store.add_command(slow)
    scheduler.store.update_state(
        "fast", {"last_run_ts": (now - dt.timedelta(minutes=2)).isoformat()}
    )
    scheduler.store.update_state(
        "slow", {"last_run_ts": (now - dt.timedelta(minutes=2)).isoformat()}
    )
    selected = scheduler.select_next(now)
    assert selected is not None
    assert selected.command_id == "fast"


def test_llm_required_command_skipped_when_unavailable():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    scheduler = _make_scheduler(now, llm_available=False)
    llm_cmd = CommandSpec("llm", "echo llm", True, 1, 5, {})
    local_cmd = CommandSpec("local", "echo local", False, 1, 0, {})
    scheduler.store.add_command(llm_cmd)
    scheduler.store.add_command(local_cmd)
    selected = scheduler.select_next(now)
    assert selected is not None
    assert selected.command_id == "local"


def test_global_rate_limiter_blocks_selection():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    scheduler = _make_scheduler(now, llm_available=True)
    cmd = CommandSpec("cmd", "echo cmd", False, 1, 0, {})
    scheduler.store.add_command(cmd)
    scheduler.store.update_global_start(now - dt.timedelta(seconds=10))
    selected = scheduler.select_next(now)
    assert selected is None


def test_scheduler_runs_commands_from_start_cwd(tmp_path):
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
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        scheduler = Scheduler(
            store,
            config,
            llm_probe=lambda _url: True,
        )

        spec = CommandSpec("cmd", "pwd", False, 1, 0, {})
        scheduler.store.add_command(spec)
        run = scheduler.start_command(spec, now)
    finally:
        os.chdir(original_cwd)

    run = run
    assert run is not None
    output = getattr(run, "output", "")
    assert output.strip() == str(tmp_path)
