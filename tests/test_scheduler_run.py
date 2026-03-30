"""Tests for the enhanced 'ampa run' CLI command."""

import argparse
import datetime as dt
import json
import io
import sys
import threading
from unittest import mock

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    RunResult,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_cli import (
    _cli_run,
    _format_run_result_json,
    _format_run_result_human,
    _format_command_detail,
    _format_command_details_table,
    _get_instance_name,
)


class DummyStore(SchedulerStore):
    def __init__(self):
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self):
        return None


def _make_spec(
    command_id="test-cmd",
    command="echo hello",
    title="Test Command",
    command_type="shell",
    frequency_minutes=10,
    priority=0,
):
    return CommandSpec(
        command_id=command_id,
        command=command,
        requires_llm=False,
        frequency_minutes=frequency_minutes,
        priority=priority,
        metadata={},
        title=title,
        command_type=command_type,
    )


def _make_run_result(exit_code=0, output="hello world"):
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 5, tzinfo=dt.timezone.utc)
    return CommandRunResult(
        start_ts=start,
        end_ts=end,
        exit_code=exit_code,
        output=output,
    )


def _make_args(**kwargs):
    """Build an argparse.Namespace with defaults for the run command."""
    defaults = {
        "command_id": None,
        "json": False,
        "verbose": False,
        "format": "normal",
        "watch": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---- _format_run_result_json ----


def test_format_run_result_json_success():
    spec = _make_spec()
    run = _make_run_result(exit_code=0)
    result = json.loads(_format_run_result_json(spec, run, "myhost"))
    assert result["id"] == "test-cmd"
    assert result["name"] == "Test Command"
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["output"] == "hello world"
    assert result["instance"] == "myhost"
    assert result["started_at"] is not None
    assert result["finished_at"] is not None


def test_format_run_result_json_failure():
    spec = _make_spec()
    run = _make_run_result(exit_code=1, output="error occurred")
    result = json.loads(_format_run_result_json(spec, run, "host"))
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["output"] == "error occurred"


def test_format_run_result_json_no_output():
    """RunResult (not CommandRunResult) has no output field."""
    spec = _make_spec()
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 3, tzinfo=dt.timezone.utc)
    run = RunResult(start_ts=start, end_ts=end, exit_code=0)
    result = json.loads(_format_run_result_json(spec, run, "host"))
    assert result["output"] is None
    assert result["exit_code"] == 0


def test_format_run_result_json_includes_pr_monitor_metadata():
    spec = _make_spec("pr-monitor", command_type="pr-monitor", title="PR Monitor")
    run = _make_run_result(exit_code=0, output="summary")
    run = CommandRunResult(
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        exit_code=run.exit_code,
        output=run.output,
        metadata={
            "pr_monitor": {
                "open_prs": 3,
                "ready_prs": [1, 2],
                "failing_prs": [3],
                "skipped_prs": [],
                "llm_reviews_dispatched": 2,
                "llm_reviews_presented": 1,
                "notifications_sent": 4,
                "auto_review_enabled": True,
            }
        },
    )
    result = json.loads(_format_run_result_json(spec, run, "host"))
    assert "pr_monitor" in result
    assert result["pr_monitor"]["open_prs"] == 3
    assert result["pr_monitor"]["llm_reviews_dispatched"] == 2


# ---- _format_run_result_human ----


def test_format_human_concise():
    spec = _make_spec()
    run = _make_run_result(exit_code=0)
    text = _format_run_result_human(spec, run, "concise", "host")
    assert "test-cmd" in text
    assert "OK" in text
    assert "5.000s" in text


def test_format_human_concise_failure():
    spec = _make_spec()
    run = _make_run_result(exit_code=42)
    text = _format_run_result_human(spec, run, "concise", "host")
    assert "FAIL(42)" in text


def test_format_human_normal():
    spec = _make_spec()
    run = _make_run_result(exit_code=0)
    text = _format_run_result_human(spec, run, "normal", "host")
    assert "Command:   test-cmd" in text
    assert "Name:      Test Command" in text
    assert "Status:    success" in text
    assert "Exit code: 0" in text
    # Normal does NOT include output
    assert "hello world" not in text


def test_format_human_normal_with_pr_monitor_metrics():
    spec = _make_spec("pr-monitor", command_type="pr-monitor", title="PR Monitor")
    run = _make_run_result(exit_code=0, output="summary")
    run = CommandRunResult(
        start_ts=run.start_ts,
        end_ts=run.end_ts,
        exit_code=run.exit_code,
        output=run.output,
        metadata={
            "pr_monitor": {
                "open_prs": 5,
                "ready_prs": [11, 12],
                "failing_prs": [13],
                "skipped_prs": [14, 15],
                "llm_reviews_dispatched": 2,
                "llm_reviews_presented": 1,
                "notifications_sent": 3,
                "auto_review_enabled": True,
            }
        },
    )
    text = _format_run_result_human(spec, run, "normal", "host")
    assert "Open PRs:  5" in text
    assert "Ready:     2" in text
    assert "Failing:   1" in text
    assert "Skipped:   2" in text
    assert "LLM Reviews: dispatched=2, presented=1" in text
    assert "Notify:    sent=3" in text
    assert "AutoReview:true" in text


def test_format_human_full():
    spec = _make_spec()
    run = _make_run_result(exit_code=0)
    text = _format_run_result_human(spec, run, "full", "host")
    assert "Instance:  host" in text
    assert "Type:      shell" in text
    assert "Command:   echo hello" in text
    assert "--- output ---" in text
    assert "hello world" in text
    assert "--- end output ---" in text


def test_format_human_full_no_output():
    """Full format with empty output shows '(no output)'."""
    spec = _make_spec()
    run = _make_run_result(exit_code=0, output="")
    text = _format_run_result_human(spec, run, "full", "host")
    assert "(no output)" in text


def test_format_human_raw():
    spec = _make_spec()
    run = _make_run_result(exit_code=0, output="raw output here")
    text = _format_run_result_human(spec, run, "raw", "host")
    assert text == "raw output here"


# ---- _format_command_detail ----


def test_format_command_detail_basic():
    spec = _make_spec()
    state = {
        "last_run_ts": "2026-01-01T12:00:00+00:00",
        "last_exit_code": 0,
        "running": False,
    }
    detail = _format_command_detail(spec, state, "normal")
    assert detail["id"] == "test-cmd"
    assert detail["name"] == "Test Command"
    assert detail["type"] == "shell"
    assert detail["frequency_minutes"] == 10
    assert detail["running"] is False
    assert detail["last_exit_code"] == 0
    assert detail["last_run"] is not None
    assert detail["next_run"] is not None


def test_format_command_detail_never_run():
    spec = _make_spec()
    state = {}
    detail = _format_command_detail(spec, state, "normal")
    assert detail["last_run"] is None
    assert detail["next_run"] is None
    assert detail["running"] is False


# ---- _format_command_details_table ----


def test_format_details_table_empty():
    assert _format_command_details_table([], "normal") == "No commands configured."


def test_format_details_table_concise():
    details = [
        {
            "id": "cmd1",
            "name": "Command One",
            "running": False,
        },
        {
            "id": "cmd2",
            "name": "Command Two",
            "running": True,
        },
    ]
    text = _format_command_details_table(details, "concise")
    assert "cmd1" in text
    assert "idle" in text
    assert "cmd2" in text
    assert "running" in text


def test_format_details_table_normal():
    details = [
        {
            "id": "cmd1",
            "name": "Command One",
            "description": "Test desc",
            "last_run": "2026-01-01T12:00:00+00:00",
            "next_run": "2026-01-01T12:10:00+00:00",
            "running": False,
        },
    ]
    text = _format_command_details_table(details, "normal")
    assert "ID:" in text
    assert "cmd1" in text
    assert "Command One" in text
    assert "Test desc" in text


def test_format_details_table_full():
    details = [
        {
            "id": "cmd1",
            "name": "Command One",
            "description": "Test",
            "type": "shell",
            "frequency_minutes": 10,
            "priority": 5,
            "requires_llm": False,
            "running": False,
            "last_run": None,
            "last_exit_code": None,
            "next_run": None,
        },
    ]
    text = _format_command_details_table(details, "full")
    assert "Type:" in text
    assert "Frequency:" in text
    assert "Priority:" in text
    assert "Requires LLM:" in text


# ---- _cli_run integration tests ----


def _capture_cli_run(args_ns):
    """Run _cli_run and capture its stdout."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        with mock.patch("ampa.scheduler_cli.daemon") as mock_daemon:
            mock_daemon.load_env = mock.MagicMock()
            exit_code = _cli_run(args_ns)
    finally:
        sys.stdout = old_stdout
    return exit_code, captured.getvalue()


def test_cli_run_list_no_command_id():
    """When no command-id is given, list all commands."""
    store = DummyStore()
    store.add_command(_make_spec("cmd-a", title="Alpha"))
    store.add_command(_make_spec("cmd-b", title="Beta"))

    args = _make_args()
    with mock.patch("ampa.scheduler_cli._store_from_env", return_value=store):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "cmd-a" in output
    assert "cmd-b" in output


def test_cli_run_list_json():
    """Listing with --json produces valid JSON array."""
    store = DummyStore()
    store.add_command(_make_spec("cmd-a", title="Alpha"))

    args = _make_args(json=True)
    with mock.patch("ampa.scheduler_cli._store_from_env", return_value=store):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == "cmd-a"
    assert data[0]["name"] == "Alpha"
    assert "type" in data[0]
    assert "frequency_minutes" in data[0]


def test_cli_run_list_empty():
    """Listing with no commands shows 'No commands configured.'"""
    store = DummyStore()
    args = _make_args()
    with mock.patch("ampa.scheduler_cli._store_from_env", return_value=store):
        exit_code, output = _capture_cli_run(args)
    assert exit_code == 0
    assert "No commands configured" in output


def test_cli_run_success():
    """Running a command by id returns exit code and formatted output."""
    spec = _make_spec("test-cmd")
    run_result = _make_run_result(exit_code=0, output="done")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    def mock_executor(_spec):
        return run_result

    scheduler = Scheduler(store, config, executor=mock_executor)

    args = _make_args(command_id="test-cmd")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "test-cmd" in output
    assert "success" in output


def test_cli_run_failure():
    """Running a command that fails returns non-zero exit code."""
    spec = _make_spec("fail-cmd")
    run_result = _make_run_result(exit_code=42, output="oops")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="fail-cmd")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 42
    assert "fail-cmd" in output


def test_cli_run_json_output_shape():
    """--json produces valid JSON with all required fields."""
    spec = _make_spec("json-cmd")
    run_result = _make_run_result(exit_code=0, output="json output")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="json-cmd", json=True)
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    data = json.loads(output)
    required_fields = [
        "id",
        "name",
        "status",
        "started_at",
        "finished_at",
        "exit_code",
        "output",
        "instance",
    ]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"
    assert data["id"] == "json-cmd"
    assert data["status"] == "success"
    assert data["output"] == "json output"


def test_cli_run_unknown_command():
    """Running an unknown command-id returns exit code 2."""
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: None)

    args = _make_args(command_id="nonexistent")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 2
    assert "Unknown command id" in output


def test_cli_run_unknown_command_json():
    """Unknown command with --json returns JSON error."""
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: None)

    args = _make_args(command_id="nonexistent", json=True)
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 2
    data = json.loads(output)
    assert "error" in data


def test_cli_run_format_concise():
    """--format concise produces single-line output."""
    spec = _make_spec("concise-cmd")
    run_result = _make_run_result(exit_code=0)

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="concise-cmd", format="concise")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    lines = output.strip().split("\n")
    assert len(lines) == 1
    assert "concise-cmd" in lines[0]
    assert "OK" in lines[0]


def test_cli_run_format_raw():
    """--format raw prints raw command output only."""
    spec = _make_spec("raw-cmd")
    run_result = _make_run_result(exit_code=0, output="raw content only")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="raw-cmd", format="raw")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert output.strip() == "raw content only"


def test_cli_run_format_full():
    """--format full includes metadata and output."""
    spec = _make_spec("full-cmd")
    run_result = _make_run_result(exit_code=0, output="full output here")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="full-cmd", format="full")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "Instance:" in output
    assert "Type:" in output
    assert "--- output ---" in output
    assert "full output here" in output


def test_cli_run_watch_mocked():
    """--watch reruns the command and can be interrupted."""
    spec = _make_spec("watch-cmd")
    call_count = 0

    def counting_executor(_spec):
        nonlocal call_count
        call_count += 1
        return _make_run_result(exit_code=0, output=f"run {call_count}")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=counting_executor)

    # Raise KeyboardInterrupt from time.sleep after 2 iterations.
    # start_command() catches BaseException (including KeyboardInterrupt)
    # so we must interrupt from outside the executor — time.sleep is
    # called between iterations in the watch loop.
    sleep_count = 0

    def interruptible_sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise KeyboardInterrupt()

    args = _make_args(command_id="watch-cmd", watch=1)
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        with mock.patch(
            "ampa.scheduler_cli.time.sleep", side_effect=interruptible_sleep
        ):
            exit_code, output = _capture_cli_run(args)

    # The watch loop runs the command, sleeps, runs again, sleeps, and the
    # second sleep raises KeyboardInterrupt which the loop catches.
    assert call_count >= 2
    assert "watch-cmd" in output


def test_cli_run_exception_handling():
    """When execution raises, error is reported gracefully."""
    spec = _make_spec("err-cmd")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    def exploding_executor(_spec):
        raise RuntimeError("boom")

    scheduler = Scheduler(store, config, executor=exploding_executor)

    args = _make_args(command_id="err-cmd")
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    # start_command wraps exceptions into RunResult so exit_code should be non-zero
    # The scheduler.start_command itself catches BaseException; if it produces
    # a RunResult the CLI should format it. If not, the CLI catches Exception
    # and returns 1.
    assert exit_code != 0


def test_cli_run_exception_json():
    """When execution raises with --json, error is JSON-formatted."""
    spec = _make_spec("err-json-cmd")

    store = DummyStore()
    store.add_command(spec)

    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    # Use an executor that raises to test the error-in-start_command path.
    # Note: Scheduler.start_command itself wraps BaseException, so the _cli_run
    # exception handler may or may not fire. Either way, output should be valid.
    def exploding_executor(_spec):
        raise RuntimeError("boom")

    scheduler = Scheduler(store, config, executor=exploding_executor)

    args = _make_args(command_id="err-json-cmd", json=True)
    with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
        exit_code, output = _capture_cli_run(args)

    # Should be valid JSON (either error or result with non-zero exit_code)
    data = json.loads(output)
    assert isinstance(data, dict)


def test_get_instance_name():
    """_get_instance_name returns a string."""
    name = _get_instance_name()
    assert isinstance(name, str)
    assert len(name) > 0


def test_cli_run_list_format_concise():
    """Listing with --format concise shows compact output."""
    store = DummyStore()
    store.add_command(_make_spec("cmd-x", title="X"))

    args = _make_args(format="concise")
    with mock.patch("ampa.scheduler_cli._store_from_env", return_value=store):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "cmd-x" in output
    assert "idle" in output


def test_cli_run_list_format_full():
    """Listing with --format full shows detailed output."""
    store = DummyStore()
    store.add_command(_make_spec("cmd-y", title="Y", command_type="delegation"))

    args = _make_args(format="full")
    with mock.patch("ampa.scheduler_cli._store_from_env", return_value=store):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "cmd-y" in output
    assert "Type:" in output
    assert "delegation" in output


def test_scheduler_store_raises_on_missing_file(tmp_path):
    """SchedulerStore must raise FileNotFoundError when the store file does not exist."""
    missing_path = tmp_path / "nonexistent" / "scheduler_store.json"
    import pytest

    with pytest.raises(FileNotFoundError, match="Scheduler store not found"):
        SchedulerStore(str(missing_path))


def test_scheduler_store_raises_on_invalid_json(tmp_path):
    """SchedulerStore must raise on corrupt/unreadable store files."""
    bad_path = tmp_path / "scheduler_store.json"
    bad_path.write_text("NOT VALID JSON")
    import pytest

    with pytest.raises(Exception):
        SchedulerStore(str(bad_path))


def test_scheduler_config_from_env_uses_local_path_only(monkeypatch, tmp_path):
    """from_env() must resolve only to <cwd>/.worklog/ampa/scheduler_store.json."""
    monkeypatch.chdir(tmp_path)
    # Ensure env var is NOT honoured
    monkeypatch.delenv("AMPA_SCHEDULER_STORE", raising=False)

    config = SchedulerConfig.from_env()
    expected = str(tmp_path / ".worklog" / "ampa" / "scheduler_store.json")
    assert config.store_path == expected


def test_scheduler_config_from_env_ignores_env_var(monkeypatch, tmp_path):
    """from_env() must ignore the AMPA_SCHEDULER_STORE env var."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AMPA_SCHEDULER_STORE", "/some/other/path.json")

    config = SchedulerConfig.from_env()
    expected = str(tmp_path / ".worklog" / "ampa" / "scheduler_store.json")
    assert config.store_path == expected

# ---------------------------------------------------------------------------
# _try_daemon_run tests
# ---------------------------------------------------------------------------

from ampa.scheduler_cli import _try_daemon_run, _daemon_port


def test_daemon_port_default(monkeypatch):
    """_daemon_port returns 8000 by default."""
    monkeypatch.delenv("AMPA_METRICS_PORT", raising=False)
    assert _daemon_port() == 8000


def test_daemon_port_from_env(monkeypatch):
    """_daemon_port reads AMPA_METRICS_PORT from environment."""
    monkeypatch.setenv("AMPA_METRICS_PORT", "9090")
    assert _daemon_port() == 9090


def test_try_daemon_run_no_daemon(monkeypatch):
    """_try_daemon_run returns None when daemon is not reachable."""
    # Point at a port where nothing is listening.
    monkeypatch.setenv("AMPA_METRICS_PORT", "19999")
    result = _try_daemon_run("any-cmd")
    assert result is None


def test_try_daemon_run_zero_port(monkeypatch):
    """_try_daemon_run returns None when port is 0 (disabled)."""
    monkeypatch.setenv("AMPA_METRICS_PORT", "0")
    result = _try_daemon_run("any-cmd")
    assert result is None


def test_cli_run_delegates_to_daemon(monkeypatch):
    """_cli_run uses daemon result when _try_daemon_run succeeds."""
    import datetime as dt

    daemon_response = {
        "id": "my-cmd",
        "name": "My Command",
        "status": "success",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:03+00:00",
        "duration_seconds": 3.0,
        "exit_code": 0,
        "output": "hello from daemon",
        "instance": "daemon-host",
    }

    args = _make_args(command_id="my-cmd")
    with mock.patch("ampa.scheduler_cli._try_daemon_run", return_value=daemon_response):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    # Human-readable output should mention the command
    assert "my-cmd" in output


def test_cli_run_daemon_json_output(monkeypatch):
    """When --json is set and daemon handles the run, CLI prints daemon JSON."""
    daemon_response = {
        "id": "my-cmd",
        "name": "My Command",
        "status": "success",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:03+00:00",
        "duration_seconds": 3.0,
        "exit_code": 0,
        "output": "daemon output",
        "instance": "daemon-host",
    }

    args = _make_args(command_id="my-cmd", json=True)
    with mock.patch("ampa.scheduler_cli._try_daemon_run", return_value=daemon_response):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    data = json.loads(output)
    assert data["id"] == "my-cmd"
    assert data["output"] == "daemon output"


def test_cli_run_falls_back_when_daemon_unavailable():
    """_cli_run falls back to local execution when daemon returns None."""
    spec = _make_spec("fallback-cmd")
    run_result = _make_run_result(exit_code=0, output="local run")
    store = DummyStore()
    store.add_command(spec)
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    args = _make_args(command_id="fallback-cmd")
    with mock.patch("ampa.scheduler_cli._try_daemon_run", return_value=None):
        with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
            exit_code, output = _capture_cli_run(args)

    assert exit_code == 0
    assert "fallback-cmd" in output


def test_cli_run_daemon_error_response(monkeypatch):
    """_cli_run handles an error dict from the daemon gracefully."""
    daemon_response = {"error": "Unknown command id: no-cmd"}

    args = _make_args(command_id="no-cmd")
    with mock.patch("ampa.scheduler_cli._try_daemon_run", return_value=daemon_response):
        exit_code, output = _capture_cli_run(args)

    assert exit_code == 2
    assert "Unknown command id" in output


def test_cli_run_watch_skips_daemon():
    """Watch mode bypasses daemon detection and runs locally."""
    spec = _make_spec("watch-daemon-cmd")
    run_result = _make_run_result(exit_code=0, output="watch output")
    store = DummyStore()
    store.add_command(spec)
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    scheduler = Scheduler(store, config, executor=lambda _: run_result)

    # Interrupt via time.sleep (same pattern as test_cli_run_watch_mocked).
    sleep_count = 0

    def interruptible_sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 1:
            raise KeyboardInterrupt()

    # _try_daemon_run should NOT be called in watch mode
    try_daemon_mock = mock.MagicMock(return_value={"id": "x", "exit_code": 0})
    args = _make_args(command_id="watch-daemon-cmd", watch=1)
    with mock.patch("ampa.scheduler_cli._try_daemon_run", try_daemon_mock):
        with mock.patch("ampa.scheduler_cli.load_scheduler", return_value=scheduler):
            with mock.patch("ampa.scheduler_cli.time.sleep", side_effect=interruptible_sleep):
                _capture_cli_run(args)

    try_daemon_mock.assert_not_called()
