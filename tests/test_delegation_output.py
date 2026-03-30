import datetime as dt
import json
import subprocess

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.engine.core import EngineConfig
from ampa.engine.dispatch import DispatchResult


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def _make_scheduler(run_shell_callable, tmp_path):
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=1,
        global_min_interval_seconds=1,
        priority_weight=0.1,
        store_path=str(tmp_path / "store.json"),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    def _executor(_spec):
        now = dt.datetime.now(dt.timezone.utc)
        return CommandRunResult(start_ts=now, end_ts=now, exit_code=0, output="")

    return Scheduler(
        store,
        config,
        run_shell=run_shell_callable,
        command_cwd=str(tmp_path),
        executor=_executor,
    )


def _delegation_spec():
    return CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation Report",
        command_type="delegation",
    )


def test_delegation_in_progress_prints_single_line(tmp_path, capsys):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps({"workItems": [{"id": "SA-1", "title": "Busy"}]})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Found 1 in progress", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {"workItem": {"id": "SA-9", "title": "Next", "stage": "idea"}}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert (
        out.strip()
        == "There is work in progress and thus no new work will be delegated."
    )


def test_delegation_idle_prints_candidate_summary(tmp_path, capsys, monkeypatch):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "workItem": {
                    "id": "SA-42",
                    "title": "Do thing",
                    "status": "open",
                    "stage": "idea",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if "wl show" in cmd and "SA-42" in cmd:
            item = {
                "id": "SA-42",
                "title": "Do thing",
                "status": "open",
                "stage": "idea",
                "priority": 2,
                "assignee": "alex",
                "description": (
                    "This is a sufficiently long description for the Do thing "
                    "work item that satisfies the engine requires_work_item_context "
                    "invariant needing more than 100 characters.\n\n"
                    "Acceptance Criteria:\n"
                    "- [ ] Summary is printed correctly\n"
                    "- [ ] Markdown includes JSON block"
                ),
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
    sched = _make_scheduler(fake_run_shell, tmp_path)

    # Override fallback_mode to None so engine uses natural stage-to-action
    sched.engine._config = EngineConfig(  # type: ignore[union-attr]
        descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
        fallback_mode=None,
    )

    # Mock engine dispatcher to avoid real subprocess spawning
    def fake_dispatch(command, work_item_id):
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            pid=99999,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    monkeypatch.setattr(sched.engine._dispatcher, "dispatch", fake_dispatch)  # type: ignore[union-attr]

    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert "Starting work on: Do thing - SA-42" in out


def test_delegation_idle_prints_concise_format_regardless_of_show(
    tmp_path, capsys, monkeypatch
):
    """Verify the concise format is used even when wl show would fail."""

    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "workItem": {
                    "id": "SA-99",
                    "title": "Fallback",
                    "status": "open",
                    "stage": "idea",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if "wl show" in cmd and "SA-99" in cmd:
            # wl show returns valid data for engine processing
            item = {
                "workItem": {
                    "id": "SA-99",
                    "title": "Fallback",
                    "status": "open",
                    "stage": "idea",
                    "description": (
                        "A sufficiently long description that satisfies the "
                        "engine requires_work_item_context invariant needing "
                        "more than 100 characters in the description field."
                    ),
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
    sched = _make_scheduler(fake_run_shell, tmp_path)

    # Override fallback_mode to None so engine uses natural stage-to-action
    sched.engine._config = EngineConfig(  # type: ignore[union-attr]
        descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
        fallback_mode=None,
    )

    # Mock engine dispatcher to avoid real subprocess spawning
    def fake_dispatch(command, work_item_id):
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            pid=99999,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    monkeypatch.setattr(sched.engine._dispatcher, "dispatch", fake_dispatch)  # type: ignore[union-attr]

    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert "Starting work on: Fallback - SA-99" in out
