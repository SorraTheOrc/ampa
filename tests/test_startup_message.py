import json
from types import SimpleNamespace

import pytest

import ampa.scheduler as sched_mod
from ampa.scheduler_types import SchedulerConfig
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore


def test_post_startup_message_uses_wl_status(tmp_path, monkeypatch):
    # prepare a minimal store file
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"commands": {}, "state": {}}))
    store = SchedulerStore(str(store_path))

    # build a simple config
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=str(store_path),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=50,
    )

    # fake run_shell that returns wl status output on stdout
    def fake_run_shell(cmd, shell, check, capture_output, text, cwd, timeout=None):
        return SimpleNamespace(
            returncode=0, stdout="WL status: all good\n1 in_progress\n", stderr=""
        )

    # capture notification calls
    captured = {}

    def fake_notify(title="", body="", message_type="other", **kwargs):
        captured["title"] = title
        captured["body"] = body
        captured["message_type"] = message_type
        return True

    # set the bot token so _post_startup_message proceeds
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    # replace notify on the imported notifications_module used by scheduler
    monkeypatch.setattr(sched_mod.notifications_module, "notify", fake_notify)

    sched = Scheduler(
        store, config, run_shell=fake_run_shell, command_cwd=str(tmp_path)
    )

    # call the protected method directly and assert the notification contains the wl status text
    sched._post_startup_message()

    assert "title" in captured, "notify() was not called"
    assert captured["title"] == "Scheduler Started"
    assert "WL status: all good" in captured["body"]
    assert captured["message_type"] == "startup"
