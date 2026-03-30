import json
import subprocess
import types

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
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
        now = CommandRunResult.__dataclass_fields__  # cheap no-op to satisfy type
        # Return a minimal successful run result
        from datetime import datetime, timezone

        now_dt = datetime.now(timezone.utc)
        return CommandRunResult(start_ts=now_dt, end_ts=now_dt, exit_code=0, output="")

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


def test_idle_delegation_posts_single_detailed_notification(tmp_path, monkeypatch):
    calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        calls.append((title, body, message_type))
        return True

    # patch the notifications module used by scheduler
    import ampa.scheduler as schedmod

    fake_mod = types.SimpleNamespace(notify=fake_notify)
    monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
    # ensure scheduler believes notifications are configured
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    def fake_run_shell(cmd, **kwargs):
        s = cmd.strip()
        if s == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if s == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        if s.startswith("wl next") and "--json" in s:
            # First candidate unsupported stage, second explicitly do-not-delegate
            payload = {
                "items": [
                    {"id": "SA-unsupported", "title": "Unsupported", "stage": "closed"},
                    {
                        "id": "SA-skip",
                        "title": "Skip me",
                        "stage": "idea",
                        "tags": ["do-not-delegate"],
                    },
                ]
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    spec = _delegation_spec()

    result_run = sched.start_command(spec)

    # Ensure a detailed idle notification is sent. When all candidates are
    # rejected the pre-dispatch report path runs and sends a report
    # that includes the considered candidates.
    assert len(calls) >= 1, "Expected at least one notification to be sent"
    title, body, mtype = calls[0]
    assert mtype == "command"
    # body content should include the considered candidates in the report
    content = body
    assert "SA-unsupported" in content and "SA-skip" in content
