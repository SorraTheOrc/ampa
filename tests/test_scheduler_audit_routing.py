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
    notify_calls = []
    work_id = "ROUTE-001"
    current_status = "in_progress"
    current_stage = "in_review"

    def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})
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
            # Expect the canonical "done" stage for completed items set by
            # the audit handlers.
            if "--stage done" in cmd_s:
                current_stage = "done"
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
    assert any(c.startswith(f"wl update {work_id}") and ("--stage done" in c) for c in calls)
    assert any(c.startswith("gh pr view 42 --repo example/repo --json merged") for c in calls)

    payload_calls = [
        c for c in notify_calls if isinstance(c.get("kwargs", {}).get("payload"), dict)
    ]
    assert payload_calls
    payload = payload_calls[0]["kwargs"]["payload"]
    content = payload.get("content", "")
    assert f"# Routing item [{work_id}]" in content
    assert "- Ready to close: YES" in content
    assert "- Criteria: 1 met, 0 partial, 0 unmet (1 total)" in content
    attachments = payload.get("attachments", [])
    assert attachments
    assert attachments[0].get("filename") == f"audit-{work_id}.md"
    assert "# AMPA Audit Result" in attachments[0].get("content", "")


def test_scheduler_audit_non_closure_notification_has_failed_criteria(
    tmp_path, monkeypatch
):
    calls = []
    notify_calls = []
    work_id = "ROUTE-002"
    current_status = "in_progress"
    current_stage = "in_review"

    def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})
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
                            "title": "Routing item no close",
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
                        "title": "Routing item no close",
                        "status": current_status,
                        "stage": current_stage,
                        "tags": [],
                    },
                    "comments": [
                        {
                            "comment": "# AMPA Audit Result\n\nCan this item be closed? No.",
                            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    ],
                    "children": [],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd_s.startswith(f'opencode run "/audit {work_id}"'):
            out = (
                "--- AUDIT REPORT START ---\n"
                "## Summary\n\nHas gaps.\n\n"
                "## Acceptance Criteria Status\n\n"
                "| # | Criterion | Verdict | Evidence |\n"
                "|---|-----------|---------|----------|\n"
                "| 1 | Ready | met | tests |\n"
                "| 2 | Docs | unmet | missing readme |\n\n"
                "## Recommendation\n\n"
                "Can this item be closed? No. Missing docs.\n"
                "--- AUDIT REPORT END ---\n"
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd_s.startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(cmd, 0, '{"success": true}', "")

        if cmd_s.startswith(f"wl update {work_id}"):
            if "--status in_progress" in cmd_s:
                current_status = "in_progress"
            if "--stage audit_failed" in cmd_s:
                current_stage = "audit_failed"
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

    assert any(c.startswith(f"wl comment add {work_id}") for c in calls)
    assert any(c.startswith(f"wl update {work_id}") and "--stage audit_failed" in c for c in calls)
    assert not any(c.startswith("gh pr view") for c in calls)

    payload_calls = [
        c for c in notify_calls if isinstance(c.get("kwargs", {}).get("payload"), dict)
    ]
    assert payload_calls
    payload = payload_calls[0]["kwargs"]["payload"]
    content = payload.get("content", "")
    assert f"# Routing item no close [{work_id}]" in content
    assert "- Ready to close: NO" in content
    assert "## Failed acceptance criteria" in content
    assert "[2] Docs | verdict: unmet | evidence: missing readme" in content
    attachments = payload.get("attachments", [])
    assert attachments
    assert attachments[0].get("filename") == f"audit-{work_id}.md"
    assert "| 2 | Docs | unmet | missing readme |" in attachments[0].get("content", "")


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
