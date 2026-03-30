import json
import json
import subprocess

from ampa.scheduler_types import CommandSpec, SchedulerConfig
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa import notifications


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
    return Scheduler(
        store, config, run_shell=run_shell_callable, command_cwd=str(tmp_path)
    )


def test_dry_run_report_and_discord_message(tmp_path, monkeypatch):
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress":
            out = "Found 1 in_progress work item(s):\n\n- Example item - SA-123"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "items": [
                    {"id": "SA-999", "title": "Next work", "status": "open"},
                    {"id": "SA-555", "title": "Later work"},
                ]
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    captured = {}

    def fake_notify(title, body="", message_type="other", *, payload=None):
        captured["title"] = title
        captured["body"] = body
        captured["message_type"] = message_type
        if payload is not None:
            captured["payload"] = payload
        return True

    monkeypatch.setattr(notifications, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation Report",
        command_type="delegation",
    )

    report = sched._delegation_orchestrator.run_delegation_report(spec)
    assert report is not None
    assert "Example item - SA-123" in report
    # when in-progress items exist, report should be concise and not include the
    # old 'AMPA Delegation' header
    assert "Agents are currently busy with:" in report
    assert "AMPA Delegation" not in report
    assert "Next work - SA-999" not in report

    message = sched._delegation_orchestrator.run_delegation_report(spec)
    assert message is not None
    payload = notifications.build_command_payload(
        "host",
        "2026-01-01T00:00:00+00:00",
        "delegation",
        message,
        0,
        title="Delegation Report",
    )
    notifications.notify(
        title="Delegation Report",
        message_type="command",
        payload=payload,
    )

    assert captured["message_type"] == "command"
