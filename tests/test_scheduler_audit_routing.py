import datetime as dt
import json
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


def test_scheduler_audit_routes_to_descriptor_handlers(tmp_path, monkeypatch):
    calls = []
    work_id = "ROUTE-001"
    current_status = "in_progress"
    current_stage = "in_review"

    def fake_notify(*args, **kwargs):
        return True

    import ampa.notifications as notifications

    monkeypatch.setattr(notifications, "notify", fake_notify)

    def fake_run_shell(cmd, **kwargs):
        nonlocal current_status, current_stage
        calls.append(cmd)
        cmd_s = cmd.strip()
        if cmd_s == "wl list --stage in_review --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Routing item",
                            "updated_at": (
                                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
                            ).isoformat(),
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd_s.startswith(f"wl show {work_id} --children --json"):
            out = json.dumps(
                {
                    "workItem": {
                        "id": work_id,
                        "title": "Routing item",
                        "status": current_status,
                        "stage": current_stage,
                        "tags": [],
                    },
                    "comments": [
                        {
                            "comment": "# AMPA Audit Result\n\nCan this item be closed? Yes.",
                            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                        },
                        {
                            "comment": "PR: https://github.com/example/repo/pull/42",
                            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                        },
                    ],
                    "children": [],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd_s.startswith(f'opencode run "/audit {work_id}"'):
            out = (
                "--- AUDIT REPORT START ---\n"
                "## Summary\n\nLooks good.\n\n"
                "## Acceptance Criteria Status\n\n"
                "| # | Criterion | Verdict | Evidence |\n"
                "|---|-----------|---------|----------|\n"
                "| 1 | Ready | met | tests |\n\n"
                "## Recommendation\n\n"
                "Can this item be closed? Yes.\n"
                "--- AUDIT REPORT END ---\n"
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd_s.startswith("gh pr view 42 --repo example/repo --json merged"):
            return subprocess.CompletedProcess(cmd, 0, '{"merged": true}', "")

        if cmd_s.startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(cmd, 0, '{"success": true}', "")

        if cmd_s.startswith(f"wl update {work_id}"):
            if "--status completed" in cmd_s:
                current_status = "completed"
            if "--stage audit_passed" in cmd_s:
                current_stage = "audit_passed"
            elif "--stage in_review" in cmd_s:
                current_stage = "in_review"
            return subprocess.CompletedProcess(cmd, 0, '{"success": true}', "")

        return subprocess.CompletedProcess(cmd, 0, "", "")

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

    sched.start_command(spec)

    assert any(c.startswith("wl list --stage in_review --json") for c in calls)
    assert any(f'/audit {work_id}' in c for c in calls)
    assert any(c.startswith(f"wl comment add {work_id}") for c in calls)
    assert any(
        c.startswith(f"wl update {work_id}") and "--stage audit_passed" in c
        for c in calls
    )
    assert any(c.startswith("gh pr view 42 --repo example/repo --json merged") for c in calls)


def test_scheduler_audit_query_failure_is_graceful(tmp_path):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl list --stage in_review --json":
            return subprocess.CompletedProcess(cmd, 1, "", "query failed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

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
    assert result is not None
