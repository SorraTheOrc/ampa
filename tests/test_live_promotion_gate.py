import argparse
import datetime as dt
import json
import os
import subprocess
from unittest import mock

import pytest

from ampa import scheduler_cli
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_types import (
    CommandSpec,
    SchedulerConfig,
    RunResult,
    CommandRunResult,
)
from ampa.engine.core import EngineResult, EngineStatus
from ampa.engine.dispatch import DispatchResult


def _make_config(store_path: str) -> SchedulerConfig:
    return SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=store_path,
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )


def _noop_executor(spec: CommandSpec) -> RunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return RunResult(start_ts=start, end_ts=end, exit_code=0)


def _noop_run_shell(cmd, **kwargs):
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")


class _SimpleSelector:
    """Return a single selected candidate so delegation proceeds."""

    def select(self):
        sel = mock.MagicMock()
        sel.raw = {"id": "WL-42", "title": "Test Work Item"}
        sel.id = "WL-42"
        sel.title = "Test Work Item"
        sel.stage = "idea"
        sel.status = "open"
        result = mock.MagicMock()
        result.global_rejections = []
        result.selected = sel
        return result


def _write_empty_store(path: str) -> None:
    d = {"commands": {}, "state": {}, "last_global_start_ts": None, "dispatches": []}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def test_cli_persist_toggle_blocks_delegation(tmp_path, monkeypatch):
    # Prepare a fresh scheduler store in a temporary project root
    project = tmp_path
    ampa_dir = project / ".worklog" / "ampa"
    ampa_dir.mkdir(parents=True)
    store_path = ampa_dir / "scheduler_store.json"
    _write_empty_store(str(store_path))

    # Change cwd so SchedulerConfig.from_env and _store_from_env use the temp store
    monkeypatch.chdir(project)

    # Use the CLI handler to set auto_assign_enabled = no
    args = argparse.Namespace(auto_assign_enabled="no")
    rc = scheduler_cli._cli_config(args)
    assert rc == 0

    # Load the persisted store and verify metadata was written
    store = SchedulerStore(str(store_path))
    spec = store.get_command("delegation")
    assert spec is not None
    assert spec.metadata.get("auto_assign_enabled") is False

    # Create a Scheduler with a mocked engine and a simple candidate selector
    engine = mock.MagicMock()
    config = _make_config(str(store_path))
    sched = Scheduler(
        store=store,
        config=config,
        executor=_noop_executor,
        run_shell=_noop_run_shell,
        engine=engine,
    )
    # Make selector available so orchestrator.inspect sees a candidate
    sched._candidate_selector = _SimpleSelector()

    # Run the delegation command; since auto_assign_enabled=false this should
    # prevent live promotions and thus engine.process_delegation should not be called.
    run = sched.start_command(spec)

    engine.process_delegation.assert_not_called()
    assert isinstance(run.metadata, dict)
    deleg = run.metadata.get("delegation")
    assert deleg is not None
    assert deleg.get("dispatched") is False
    assert "audit_only" in (deleg.get("note") or "") or "skipped" in (
        deleg.get("note") or ""
    )


def test_cli_persist_toggle_allows_delegation_and_dispatch(tmp_path, monkeypatch):
    project = tmp_path
    ampa_dir = project / ".worklog" / "ampa"
    ampa_dir.mkdir(parents=True)
    store_path = ampa_dir / "scheduler_store.json"
    _write_empty_store(str(store_path))

    monkeypatch.chdir(project)

    # Enable auto_assign via CLI
    args = argparse.Namespace(auto_assign_enabled="yes")
    rc = scheduler_cli._cli_config(args)
    assert rc == 0

    store = SchedulerStore(str(store_path))
    spec = store.get_command("delegation")
    assert spec is not None
    assert spec.metadata.get("auto_assign_enabled") is True

    # Prepare a fake engine that returns SUCCESS with a dispatch result
    from ampa.engine.dispatch import DispatchResult

    ts = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    dispatch = DispatchResult(
        success=True,
        command='opencode run "/intake WL-42 do not ask questions"',
        work_item_id="WL-42",
        timestamp=ts,
        pid=99999,
    )

    er = EngineResult(
        status=EngineStatus.SUCCESS,
        reason="",
        work_item_id="WL-42",
        command_name="delegate",
        action="intake",
        dispatch_result=dispatch,
        timestamp=ts,
    )

    engine = mock.MagicMock()
    engine.process_delegation.return_value = er

    config = _make_config(str(store_path))
    sched = Scheduler(
        store=store,
        config=config,
        executor=_noop_executor,
        run_shell=_noop_run_shell,
        engine=engine,
    )
    sched._candidate_selector = _SimpleSelector()

    run = sched.start_command(spec)

    # Engine should be invoked when auto_assign_enabled is true
    engine.process_delegation.assert_called_once()
    assert isinstance(run.metadata, dict)
    deleg = run.metadata.get("delegation")
    assert deleg is not None
    assert deleg.get("dispatched") is True
    info = deleg.get("delegate_info") or {}
    assert info.get("id") == "WL-42"
    assert info.get("action") == "intake"
