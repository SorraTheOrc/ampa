import os
import subprocess

from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_types import CommandSpec, SchedulerConfig


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "config": {},
        }

    def save(self) -> None:
        return None


def make_scheduler(run_shell_callable, tmp_path):
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=1,
        global_min_interval_seconds=1,
        priority_weight=0.1,
        store_path=str(tmp_path / "store.json"),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    return Scheduler(store, config, run_shell=run_shell_callable, command_cwd=str(tmp_path))


def test_missing_descriptor_emits_notification(tmp_path, monkeypatch):
    # Simulate missing descriptor file and assert scheduler handles it
    calls = {}

    def fake_notify(*args, **kwargs):
        # record that notify was called and the body contains the path
        calls['notify'] = kwargs or args
        return True

    import ampa.notifications as notifications

    monkeypatch.setattr(notifications, "notify", fake_notify)

    def fake_run_shell(cmd, **kwargs):
        # minimal stub; not used in this test
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("AMPA_WORKFLOW_DESCRIPTOR", str(tmp_path / "no-such-file.yaml"))

    sched = make_scheduler(fake_run_shell, tmp_path)
    spec = CommandSpec(
        command_id="wl-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="audit",
    )
    sched.store.add_command(spec)

    result = sched.start_command(spec)

    # Expect scheduler to return a RunResult (not raise) and that notify was called
    assert result is not None
    assert 'notify' in calls
