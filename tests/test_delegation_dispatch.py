import json
import subprocess
import datetime as dt
from unittest.mock import MagicMock, patch

from ampa.scheduler_types import CommandSpec, SchedulerConfig
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler import Scheduler
from ampa import notifications as notifications_module
from ampa.engine.core import EngineConfig, EngineResult, EngineStatus
from ampa.engine.dispatch import DispatchResult


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "config": {},
            "dispatches": [],
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
    sched = Scheduler(
        store, config, run_shell=run_shell_callable, command_cwd=str(tmp_path)
    )
    return sched


def test_dispatch_logged_before_spawn(tmp_path, monkeypatch):
    """Verify a dispatch record is persisted and a dispatch notification is sent
    when the engine successfully dispatches work.

    The engine records the dispatch after spawning the subprocess (via
    ``StoreDispatchRecorder``). This test patches the engine's dispatcher to
    avoid real subprocess spawning and verifies that the full delegation
    flow (inspect → engine dispatch → record → notification) works end-to-end.
    """
    calls = []
    captured = {"calls": []}

    # fake wl in_progress -> no in-progress items
    # wl next -> return a candidate with valid from-state
    # wl show -> return work item details for invariant evaluation
    # wl update -> succeed
    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        s = cmd.strip()
        if s == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if s == "wl next --json":
            payload = {
                "workItem": {
                    "id": "SA-TEST-123",
                    "title": "Idea item",
                    "status": "open",
                    "stage": "idea",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if "wl show" in s and "SA-TEST-123" in s:
            item = {
                "id": "SA-TEST-123",
                "title": "Idea item",
                "status": "open",
                "stage": "idea",
                "description": (
                    "This is a test work item for delegation testing. "
                    "It contains sufficient context to satisfy the "
                    "requires_work_item_context invariant which needs "
                    "more than 100 characters in the description.\n\n"
                    "Acceptance Criteria:\n"
                    "- [ ] Test passes end-to-end\n"
                    "- [ ] Dispatch record is persisted"
                ),
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    # Disable fallback mode override so the engine uses the natural
    # stage-to-action mapping.  The env var must be set BEFORE creating the
    # scheduler since _build_engine resolves fallback_mode at init time.
    # Setting to empty string causes resolve_mode to fall through to the
    # config-file path which defaults to auto-accept when no config exists.
    # Instead, we explicitly unset and ensure the config path exists.
    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

    sched = make_scheduler(fake_run_shell, tmp_path)

    # Override the engine's fallback_mode to None so it doesn't force
    # action=accept (which has no command template)
    sched.engine._config = EngineConfig(  # type: ignore[union-attr]
        descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
        fallback_mode=None,
    )

    # Patch the engine's dispatcher to avoid real subprocess spawning
    # and record that dispatch was called
    dispatch_state = {"called": False, "command": None}

    def fake_dispatch(command, work_item_id):
        dispatch_state["called"] = True
        dispatch_state["command"] = command
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            pid=12345,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    monkeypatch.setattr(sched.engine._dispatcher, "dispatch", fake_dispatch)  # type: ignore[union-attr]

    # capture notification calls
    def fake_notify(title, body="", message_type="other", *, payload=None):
        captured["calls"].append(
            {"title": title, "body": body, "message_type": message_type}
        )

    monkeypatch.setattr(notifications_module, "notify", fake_notify)

    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation",
        command_type="delegation",
    )
    sched.store.add_command(spec)

    # ensure notification env is present so pre-dispatch path runs
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    # disable fallback mode override so the engine uses the natural stage-to-action
    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

    # run start_command which triggers the delegation flow
    sched.start_command(spec)

    # verify the engine dispatched
    assert dispatch_state["called"] is True, "engine dispatcher was not called"
    assert dispatch_state["command"] is not None
    assert "SA-TEST-123" in dispatch_state["command"]

    # verify a dispatch record was persisted in the store
    dispatches = sched.store.data.get("dispatches", [])
    assert len(dispatches) > 0, "no dispatch record persisted"
    assert dispatches[-1].get("work_item_id") == "SA-TEST-123"

    # verify a notification was sent
    assert len(captured["calls"]) > 0, "no notification sent during delegation dispatch"
