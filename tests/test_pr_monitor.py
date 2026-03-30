"""Tests for the PR monitor scheduled command.

Verifies that:
1. PRMonitorRunner detects when gh CLI is unavailable.
2. PRMonitorRunner handles empty PR list.
3. PRMonitorRunner correctly identifies passing checks and posts ready comments.
4. PRMonitorRunner correctly identifies failing checks and creates critical work items.
5. PRMonitorRunner deduplicates ready-for-review comments.
6. PRMonitorRunner skips PRs with pending checks.
7. The scheduler routes command_type='pr-monitor' through PRMonitorRunner.
8. The pr-monitor command is auto-registered at scheduler init.
9. Error handling for gh CLI failures is robust.
10. Notifications are sent for ready and failing PRs.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.pr_monitor import PRMonitorRunner, _coerce_bool
from ampa.scheduler_types import CommandSpec, RunResult, SchedulerConfig
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
import datetime as dt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    """In-memory store for testing."""

    def __init__(self):
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "dispatches": [],
        }

    def save(self):
        return None


def _make_config(**overrides) -> SchedulerConfig:
    defaults = dict(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    defaults.update(overrides)
    return SchedulerConfig(**defaults)


def _make_pr_monitor_spec(
    command_id: str = "pr-monitor",
    dedup: bool = True,
    max_prs: int = 50,
    gh_command: str = "gh",
    auto_review: Optional[bool] = None,
) -> CommandSpec:
    metadata = {"dedup": dedup, "max_prs": max_prs, "gh_command": gh_command}
    if auto_review is not None:
        metadata["auto_review"] = auto_review
    return CommandSpec(
        command_id=command_id,
        command="echo pr-monitor",
        requires_llm=False,
        frequency_minutes=60,
        priority=0,
        metadata=metadata,
        title="PR Monitor",
        max_runtime_minutes=10,
        command_type="pr-monitor",
    )


def _noop_executor(spec: CommandSpec) -> RunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return RunResult(start_ts=start, end_ts=end, exit_code=0)


def _make_shell(responses: Dict[str, Any]):
    """Build a run_shell stub that maps command substrings to responses.

    Each value in *responses* is either:
    - A dict with optional keys ``returncode``, ``stdout``, ``stderr``
    - An exception type/instance to raise
    """

    def _shell(cmd, **kwargs):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        for prefix, resp in responses.items():
            if prefix in cmd_str:
                if isinstance(resp, BaseException) or (
                    isinstance(resp, type) and issubclass(resp, BaseException)
                ):
                    raise resp if isinstance(resp, BaseException) else resp()
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=resp.get("returncode", 0),
                    stdout=resp.get("stdout", ""),
                    stderr=resp.get("stderr", ""),
                )
        # Default: success, empty output
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return _shell


def _make_scheduler(run_shell=None) -> Scheduler:
    store = DummyStore()
    config = _make_config()
    engine_mock = mock.MagicMock()
    sched = Scheduler(
        store=store,
        config=config,
        executor=_noop_executor,
        run_shell=run_shell
        or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
        engine=engine_mock,
    )
    return sched


def _pr_list_json(prs: List[Dict[str, Any]]) -> str:
    return json.dumps(prs)


def _checks_json(checks: List[Dict[str, Any]]) -> str:
    return json.dumps(checks)


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — gh unavailable
# ---------------------------------------------------------------------------


class TestPRMonitorGhUnavailable:
    def test_gh_not_found(self):
        run_shell = _make_shell(
            {"gh --version": {"returncode": 127, "stderr": "command not found"}}
        )
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp"
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "gh_unavailable"
        assert result["prs_checked"] == 0

    def test_gh_exception(self):
        def bad_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                raise RuntimeError("gh binary not found")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=bad_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "gh_unavailable"


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — no open PRs
# ---------------------------------------------------------------------------


class TestPRMonitorNoPRs:
    def test_empty_pr_list(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": "[]"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "no_prs"
        assert result["prs_checked"] == 0

    def test_pr_list_failure(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 1, "stderr": "auth error"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "list_failed"

    def test_pr_list_invalid_json(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": "not-json"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "list_failed"

    def test_pr_list_empty_stdout(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": ""},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "no_prs"


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — passing checks (ready for review)
# ---------------------------------------------------------------------------


class TestPRMonitorReady:
    def test_all_checks_passing_posts_comments(self):
        pr_list = _pr_list_json(
            [{"number": 42, "title": "Add feature X", "url": "https://github.com/repo/pull/42", "headRefName": "feat-x"}]
        )
        checks = _checks_json(
            [
                {"name": "ci", "bucket": "pass"},
                {"name": "lint", "bucket": "pass"},
            ]
        )
        calls: Dict[str, List[str]] = {"gh_comments": [], "wl": []}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                # No existing comments with marker
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            if "gh pr comment" in cmd_str:
                calls["gh_comments"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            if "wl" in cmd_str:
                calls["wl"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "[]", "")
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert result["prs_checked"] == 1
        assert 42 in result["ready_prs"]
        assert len(result["failing_prs"]) == 0
        # Should have posted a GH comment
        assert len(calls["gh_comments"]) == 1
        assert "42" in calls["gh_comments"][0]

    def test_no_checks_configured_treated_as_passing(self):
        pr_list = _pr_list_json(
            [{"number": 10, "title": "No checks", "url": "https://github.com/repo/pull/10", "headRefName": "no-checks"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                # No checks — empty stdout, success exit
                return subprocess.CompletedProcess([], 0, "", "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 10 in result["ready_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — failing checks
# ---------------------------------------------------------------------------


class TestPRMonitorFailing:
    def test_failing_checks_creates_work_item(self):
        pr_list = _pr_list_json(
            [{"number": 55, "title": "Broken PR", "url": "https://github.com/repo/pull/55", "headRefName": "broken"}]
        )
        checks = _checks_json(
            [
                {"name": "ci-build", "bucket": "fail"},
                {"name": "lint", "bucket": "pass"},
            ]
        )
        calls: Dict[str, List] = {"wl_create": [], "gh_comments": []}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 1, checks, "")
            if "gh pr comment" in cmd_str:
                calls["gh_comments"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            if "wl create" in cmd_str:
                calls["wl_create"].append(cmd_str)
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"id": "WI-NEW"}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 55 in result["failing_prs"]
        assert len(calls["wl_create"]) == 1
        assert len(calls["gh_comments"]) == 1
        # Notifier should be called with error for failing PR
        error_calls = [
            c
            for c in notifier.notify.call_args_list
            if c.kwargs.get("message_type") == "error"
        ]
        assert len(error_calls) >= 1

    def test_multiple_failing_checks(self):
        pr_list = _pr_list_json(
            [{"number": 77, "title": "Multi fail", "url": "https://github.com/repo/pull/77", "headRefName": "multi"}]
        )
        checks = _checks_json(
            [
                {"name": "build", "bucket": "fail"},
                {"name": "test", "bucket": "fail"},
                {"name": "lint", "bucket": "pass"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 1, checks, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert 77 in result["failing_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — deduplication
# ---------------------------------------------------------------------------


class TestPRMonitorDedup:
    def test_skips_when_ready_comment_exists(self):
        pr_list = _pr_list_json(
            [{"number": 33, "title": "Already notified", "url": "https://github.com/repo/pull/33", "headRefName": "dedup"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                # Comment with marker already exists
                return subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "comments": [
                                {
                                    "body": "<!-- ampa-pr-monitor:ready -->\n## All CI checks are passing"
                                }
                            ]
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec(dedup=True)
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 33 in result["skipped_prs"]
        assert 33 not in result["ready_prs"]

    def test_does_not_skip_when_dedup_disabled(self):
        pr_list = _pr_list_json(
            [{"number": 33, "title": "Re-notify", "url": "https://github.com/repo/pull/33", "headRefName": "nodedup"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        gh_comment_calls: List[str] = []

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr comment" in cmd_str:
                gh_comment_calls.append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec(dedup=False)
        result = runner.run(spec)

        assert 33 in result["ready_prs"]
        assert len(gh_comment_calls) == 1


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — pending checks
# ---------------------------------------------------------------------------


class TestPRMonitorPending:
    def test_pending_checks_skipped(self):
        pr_list = _pr_list_json(
            [{"number": 22, "title": "Still running", "url": "https://github.com/repo/pull/22", "headRefName": "pending"}]
        )
        checks = _checks_json(
            [
                {"name": "ci", "bucket": "pending"},
                {"name": "lint", "bucket": "pass"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 22 in result["skipped_prs"]
        assert 22 not in result["ready_prs"]
        assert 22 not in result["failing_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — multiple PRs
# ---------------------------------------------------------------------------


class TestPRMonitorMultiplePRs:
    def test_mixed_ready_and_failing(self):
        pr_list = _pr_list_json(
            [
                {"number": 1, "title": "Ready PR", "url": "https://github.com/repo/pull/1", "headRefName": "ready"},
                {"number": 2, "title": "Failing PR", "url": "https://github.com/repo/pull/2", "headRefName": "fail"},
                {"number": 3, "title": "Pending PR", "url": "https://github.com/repo/pull/3", "headRefName": "pend"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                if " 1 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        _checks_json(
                            [{"name": "ci", "bucket": "pass"}]
                        ),
                        "",
                    )
                if " 2 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        1,
                        _checks_json(
                            [{"name": "ci", "bucket": "fail"}]
                        ),
                        "",
                    )
                if " 3 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        _checks_json(
                            [{"name": "ci", "bucket": "pending"}]
                        ),
                        "",
                    )
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert result["prs_checked"] == 3
        assert 1 in result["ready_prs"]
        assert 2 in result["failing_prs"]
        assert 3 in result["skipped_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — error resilience
# ---------------------------------------------------------------------------


class TestPRMonitorErrorResilience:
    def test_check_status_failure_skips_pr(self):
        pr_list = _pr_list_json(
            [{"number": 88, "title": "Error PR", "url": "https://github.com/repo/pull/88", "headRefName": "err"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                # Failure with no stdout
                return subprocess.CompletedProcess([], 1, "", "internal error")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        # PR should be silently skipped, not crash
        assert result["action"] == "completed"
        assert 88 not in result["ready_prs"]
        assert 88 not in result["failing_prs"]

    def test_gh_comment_failure_does_not_crash(self):
        pr_list = _pr_list_json(
            [{"number": 99, "title": "Comment fail", "url": "https://github.com/repo/pull/99", "headRefName": "cf"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            if "gh pr comment" in cmd_str:
                return subprocess.CompletedProcess([], 1, "", "rate limited")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        # Should still count as ready even though comment failed
        assert 99 in result["ready_prs"]

    def test_notifier_exception_does_not_crash(self):
        pr_list = _pr_list_json(
            [{"number": 11, "title": "Notify fail", "url": "https://github.com/repo/pull/11", "headRefName": "nf"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("discord down")
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        # Should not raise
        result = runner.run(spec)
        assert result["action"] == "completed"


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------


class TestSchedulerPRMonitor:
    """The scheduler correctly routes pr-monitor command types."""

    def test_scheduler_runs_pr_monitor(self):
        """Scheduler routes command_type=pr-monitor through PRMonitorRunner."""
        pr_list = _pr_list_json(
            [{"number": 5, "title": "Test", "url": "https://github.com/repo/pull/5", "headRefName": "test"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        sched = _make_scheduler(run_shell=run_shell)
        with mock.patch("ampa.scheduler.notifications_module") as notifier_mock:
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None

    def test_scheduler_exception_does_not_propagate(self):
        """An exception in the runner should not escape start_command."""

        def bad_shell(cmd, **kwargs):
            raise RuntimeError("everything is broken")

        sched = _make_scheduler(run_shell=bad_shell)
        with mock.patch("ampa.scheduler.notifications_module"):
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None

    def test_scheduler_auto_review_missing_defaults_enabled(self):
        """Missing auto_review metadata should inject dispatcher."""
        sched = _make_scheduler()
        with mock.patch("ampa.scheduler.notifications_module"), \
            mock.patch("ampa.engine.dispatch.ContainerDispatcher") as dispatcher_cls, \
            mock.patch("ampa.pr_monitor.PRMonitorRunner") as runner_cls:
            runner = runner_cls.return_value
            runner.run.return_value = {"action": "completed"}
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None
        dispatcher_cls.assert_called_once()
        kwargs = runner_cls.call_args.kwargs
        assert kwargs["dispatcher"] is dispatcher_cls.return_value

    def test_scheduler_auto_review_false_skips_dispatcher(self):
        """Explicit auto_review=False should disable dispatcher injection."""
        sched = _make_scheduler()
        with mock.patch("ampa.scheduler.notifications_module"), \
            mock.patch("ampa.engine.dispatch.ContainerDispatcher") as dispatcher_cls, \
            mock.patch("ampa.pr_monitor.PRMonitorRunner") as runner_cls:
            runner = runner_cls.return_value
            runner.run.return_value = {"action": "completed"}
            run = sched.start_command(_make_pr_monitor_spec(auto_review=False))

        assert run is not None
        dispatcher_cls.assert_not_called()
        kwargs = runner_cls.call_args.kwargs
        assert kwargs["dispatcher"] is None

    def test_scheduler_dispatcher_failure_does_not_stop_pr_monitor(self):
        """ContainerDispatcher errors should not abort pr-monitor execution."""
        sched = _make_scheduler()
        with mock.patch("ampa.scheduler.notifications_module"), \
            mock.patch(
                "ampa.engine.dispatch.ContainerDispatcher",
                side_effect=RuntimeError("boom"),
            ), \
            mock.patch("ampa.pr_monitor.PRMonitorRunner") as runner_cls:
            runner = runner_cls.return_value
            runner.run.return_value = {"action": "completed"}
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None
        kwargs = runner_cls.call_args.kwargs
        assert kwargs["dispatcher"] is None

    def test_pr_monitor_auto_registered(self):
        """Scheduler init auto-registers the pr-monitor command."""
        sched = _make_scheduler()
        cmd_ids = [c.command_id for c in sched.store.list_commands()]
        assert "pr-monitor" in cmd_ids

    def test_pr_monitor_frequency_is_hourly(self):
        """Auto-registered pr-monitor command runs every 60 minutes."""
        sched = _make_scheduler()
        cmd = sched.store.get_command("pr-monitor")
        assert cmd is not None
        assert cmd.frequency_minutes == 60

    def test_pr_monitor_metadata_defaults(self):
        """Auto-registered pr-monitor has expected metadata defaults."""
        sched = _make_scheduler()
        cmd = sched.store.get_command("pr-monitor")
        assert cmd is not None
        assert cmd.metadata.get("dedup") is True
        assert cmd.metadata.get("max_prs") == 50
        assert cmd.metadata.get("auto_review") is True


# ---------------------------------------------------------------------------
# Unit tests for _coerce_bool utility
# ---------------------------------------------------------------------------


class TestCoerceBool:
    def test_true_values(self):
        assert _coerce_bool(True) is True
        assert _coerce_bool("true") is True
        assert _coerce_bool("True") is True
        assert _coerce_bool("1") is True
        assert _coerce_bool("yes") is True
        assert _coerce_bool("on") is True

    def test_false_values(self):
        assert _coerce_bool(False) is False
        assert _coerce_bool(None) is False
        assert _coerce_bool("false") is False
        assert _coerce_bool("0") is False
        assert _coerce_bool("") is False
        assert _coerce_bool("no") is False


# ---------------------------------------------------------------------------
# Unit tests for check state parsing
# ---------------------------------------------------------------------------


class TestCheckStateParsing:
    """Verify edge cases in _get_check_status parsing."""

    def _run_with_checks(self, checks_json_str: str) -> dict:
        pr_list = _pr_list_json(
            [{"number": 1, "title": "Test", "url": "https://github.com/repo/pull/1", "headRefName": "test"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks_json_str, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        return runner.run(spec)

    def test_success_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "pass"}])
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_neutral_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_skipped_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_timed_out_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "fail"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["failing_prs"]

    def test_error_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "fail"}])
        result = self._run_with_checks(checks)
        assert 1 in result["failing_prs"]

    def test_queued_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "pending"}])
        result = self._run_with_checks(checks)
        assert 1 in result["skipped_prs"]


# ---------------------------------------------------------------------------
# Unit tests for work-item ID extraction
# ---------------------------------------------------------------------------


class TestExtractWorkItemId:
    """Verify _extract_work_item_id() handles branch names and PR bodies."""

    def _make_runner(self, run_shell=None):
        return PRMonitorRunner(
            run_shell=run_shell
            or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
            command_cwd="/tmp",
        )

    def test_feature_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "feature/SA-0MMN9YNS41N1B77L-llm-pr-review", "number": 1}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-0MMN9YNS41N1B77L"

    def test_bug_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "bug/WL-ABC123DEF0-fix-crash", "number": 2}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "WL-ABC123DEF0"

    def test_wl_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "wl-SA-0MMABCDEF12345-short", "number": 3}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-0MMABCDEF12345"

    def test_no_branch_falls_back_to_body(self):
        body_json = json.dumps({"body": "Fixes work-item: SA-TESTID1234"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "some-random-branch", "number": 5}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-TESTID1234"

    def test_body_closes_pattern(self):
        body_json = json.dumps({"body": "This PR closes WL-0ABCDEFGHIJ"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "no-match-here", "number": 6}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "WL-0ABCDEFGHIJ"

    def test_no_work_item_found(self):
        body_json = json.dumps({"body": "Just a regular PR"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "main", "number": 7}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None

    def test_missing_branch_name(self):
        runner = self._make_runner()
        pr = {"number": 8}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None

    def test_gh_view_failure(self):
        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 1, "", "error")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "no-match", "number": 9}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests for audit dispatch state tracking
# ---------------------------------------------------------------------------


class TestAuditDispatchState:
    """Verify _get_audit_dispatch_state() and _post_audit_dispatch_marker()."""

    def _make_runner(self, wl_shell=None):
        default_shell = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
        return PRMonitorRunner(
            run_shell=default_shell,
            command_cwd="/tmp",
            wl_shell=wl_shell or default_shell,
        )

    def test_no_dispatch_state(self):
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_dispatch_state_found(self):
        marker = "<!-- ampa-pr-audit-dispatch:42 -->"
        payload = json.dumps({
            "dispatch_state": {
                "pr_number": 42,
                "dispatched_at": "2026-03-12T10:00:00Z",
                "container_id": "pool-1",
                "work_item_id": "SA-TEST1",
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "ampa-pr-monitor"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is not None
        assert result["dispatch_state"]["pr_number"] == 42
        assert result["dispatch_state"]["container_id"] == "pool-1"

    def test_dispatch_state_wrong_pr(self):
        """Dispatch marker for a different PR number is not matched."""
        marker = "<!-- ampa-pr-audit-dispatch:99 -->"
        payload = json.dumps({
            "dispatch_state": {"pr_number": 99, "dispatched_at": "2026-03-12T10:00:00Z"}
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "ampa-pr-monitor"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_wl_show_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_post_dispatch_marker_success(self):
        calls = []

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            calls.append(cmd_str)
            return subprocess.CompletedProcess([], 0, "{}", "")

        runner = self._make_runner(wl_shell)
        result = runner._post_audit_dispatch_marker(
            "SA-TEST1", 42, "2026-03-12T10:00:00Z", "pool-1"
        )
        assert result is True
        assert len(calls) == 1
        assert "wl comment add SA-TEST1" in calls[0]

    def test_post_dispatch_marker_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._post_audit_dispatch_marker(
            "SA-TEST1", 42, "2026-03-12T10:00:00Z"
        )
        assert result is False


# ---------------------------------------------------------------------------
# Unit tests for audit result query
# ---------------------------------------------------------------------------


class TestAuditResult:
    """Verify _get_audit_result() and _parse_marker_json()."""

    def _make_runner(self, wl_shell=None):
        default_shell = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
        return PRMonitorRunner(
            run_shell=default_shell,
            command_cwd="/tmp",
            wl_shell=wl_shell or default_shell,
        )

    def test_no_audit_result(self):
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None

    def test_audit_result_found(self):
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "criteria": [{"name": "Tests pass", "pass": True, "notes": "All green"}],
                "summary": "All criteria met",
                "concerns": [],
                "audited_at": "2026-03-12T12:00:00Z",
                "pr_number": 42,
                "pr_sha": "abc123",
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is not None
        assert result["overall"] == "pass"
        assert result["pr_number"] == 42

    def test_audit_result_wrong_pr(self):
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T12:00:00Z",
                "pr_number": 99,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None

    def test_audit_result_stale(self):
        """Audit result older than after_iso is rejected."""
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T10:00:00Z",
                "pr_number": 42,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result(
            "SA-TEST1", 42, after_iso="2026-03-12T11:00:00Z"
        )
        assert result is None

    def test_audit_result_fresh(self):
        """Audit result newer than after_iso is accepted."""
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T14:00:00Z",
                "pr_number": 42,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result(
            "SA-TEST1", 42, after_iso="2026-03-12T11:00:00Z"
        )
        assert result is not None
        assert result["overall"] == "pass"

    def test_wl_show_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests for _parse_marker_json
# ---------------------------------------------------------------------------


class TestParseMarkerJson:
    """Verify the static _parse_marker_json helper."""

    def test_valid_json(self):
        body = '<!-- marker -->\n{"key": "value"}'
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"key": "value"}

    def test_nested_json(self):
        body = '<!-- marker -->\n{"outer": {"inner": 42}}'
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"outer": {"inner": 42}}

    def test_no_marker(self):
        body = "no marker here"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_no_json_after_marker(self):
        body = "<!-- marker -->\nno json here"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_invalid_json(self):
        body = "<!-- marker -->\n{invalid json}"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_marker_with_extra_text(self):
        body = "Some prefix\n<!-- marker -->\ntext {\"a\": 1} more"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"a": 1}


# ---------------------------------------------------------------------------
# Unit tests for _dispatch_review (Phase 1)
# ---------------------------------------------------------------------------


class TestDispatchReview:
    """Verify _dispatch_review() dispatch logic."""

    _DISPATCH_MARKER_PREFIX = "<!-- ampa-pr-audit-dispatch:"

    def _make_runner(
        self, run_shell=None, wl_shell=None, dispatcher=None
    ):
        return PRMonitorRunner(
            run_shell=run_shell
            or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
            command_cwd="/tmp",
            dispatcher=dispatcher,
            wl_shell=wl_shell,
        )

    def _make_dispatch_result(self, success=True, error=None, container_id="c1"):
        """Build a minimal DispatchResult-like object."""
        return type(
            "FakeResult",
            (),
            {
                "success": success,
                "pid": 12345 if success else None,
                "error": error,
                "container_id": container_id,
                "timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            },
        )()

    # -- no dispatcher configured -------------------------------------------

    def test_skips_when_no_dispatcher(self):
        """auto_review enabled but no dispatcher — silent skip."""
        runner = self._make_runner(dispatcher=None)
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 10}
        # Should not raise
        runner._dispatch_review("gh", pr, 10, "Some PR")

    # -- no work item ID extracted ------------------------------------------

    def test_skips_when_no_work_item_id(self):
        """Branch name doesn't contain a recognisable work-item ID."""
        fake_dispatcher = mock.MagicMock()
        runner = self._make_runner(dispatcher=fake_dispatcher)
        pr = {"headRefName": "random-branch", "number": 11}
        runner._dispatch_review("gh", pr, 11, "No ID PR")
        fake_dispatcher.dispatch.assert_not_called()

    # -- already dispatched -------------------------------------------------

    def test_skips_when_already_dispatched(self):
        """Dispatch marker already exists for this PR number."""
        dispatch_comment = json.dumps({
            "dispatch_state": {
                "pr_number": 12,
                "dispatched_at": "2026-01-01T00:00:00+00:00",
                "container_id": "c1",
                "work_item_id": "SA-TEST123456",
            }
        })
        marker = f"{self._DISPATCH_MARKER_PREFIX}12 -->"
        wl_output = json.dumps({
            "comments": [
                {"comment": f"{marker}\n{dispatch_comment}"}
            ]
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_output, "")

        fake_dispatcher = mock.MagicMock()
        runner = self._make_runner(wl_shell=wl_shell, dispatcher=fake_dispatcher)
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 12}
        runner._dispatch_review("gh", pr, 12, "Already Dispatched")
        fake_dispatcher.dispatch.assert_not_called()

    # -- successful dispatch ------------------------------------------------

    def test_successful_dispatch(self):
        """Dispatch succeeds — marker is posted."""
        result = self._make_dispatch_result(success=True, container_id="pool-1")
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = result

        wl_calls = []

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            wl_calls.append(cmd_str)
            # Return empty comments for _get_audit_dispatch_state
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(wl_shell=wl_shell, dispatcher=fake_dispatcher)
        pr = {"headRefName": "feature/SA-TEST123456-desc", "number": 13}
        runner._dispatch_review("gh", pr, 13, "Good PR")

        # Dispatcher was called
        fake_dispatcher.dispatch.assert_called_once()
        call_kwargs = fake_dispatcher.dispatch.call_args
        assert "SA-TEST123456" in call_kwargs.kwargs.get(
            "work_item_id", call_kwargs[1].get("work_item_id", "")
        ) or "SA-TEST123456" in str(call_kwargs)

        # A marker comment was posted
        marker_posts = [c for c in wl_calls if "comment" in c and "add" in c]
        assert len(marker_posts) >= 1

    # -- dispatch failure ---------------------------------------------------

    def test_dispatch_failure_logged(self):
        """Dispatch returns failure — failure is posted as comment."""
        result = self._make_dispatch_result(
            success=False, error="No pool containers available"
        )
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = result

        wl_calls = []

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            wl_calls.append(cmd_str)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(wl_shell=wl_shell, dispatcher=fake_dispatcher)
        pr = {"headRefName": "feature/SA-TEST123456-desc", "number": 14}
        runner._dispatch_review("gh", pr, 14, "Failing PR")

        # Dispatcher was called
        fake_dispatcher.dispatch.assert_called_once()

        # A failure comment was posted (not a marker)
        fail_comments = [
            c for c in wl_calls if "comment" in c and "failed" in c.lower()
        ]
        assert len(fail_comments) >= 1

    # -- dispatch exception does not propagate --------------------------------

    def test_dispatch_exception_does_not_propagate(self):
        """If the dispatcher raises, _dispatch_review catches it."""
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.side_effect = RuntimeError("boom")

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(wl_shell=wl_shell, dispatcher=fake_dispatcher)
        pr = {"headRefName": "feature/SA-TEST123456-desc", "number": 15}
        # Should not raise
        runner._dispatch_review("gh", pr, 15, "Exploding PR")


# ---------------------------------------------------------------------------
# Integration test: auto_review flag in run()
# ---------------------------------------------------------------------------


class TestAutoReviewIntegration:
    """Verify that run() dispatches reviews when auto_review is True."""

    def _make_spec(self, auto_review=False, dedup=False):
        return type(
            "FakeSpec",
            (),
            {"metadata": {"auto_review": auto_review, "dedup": dedup}},
        )()

    def _one_pr_json(self):
        return json.dumps([
            {
                "number": 99,
                "title": "Test PR",
                "url": "https://github.com/test/repo/pull/99",
                "headRefName": "feature/SA-INTEGTEST01-test",
            }
        ])

    def _all_pass_checks(self):
        return json.dumps([
            {"name": "ci", "bucket": "pass", "state": "SUCCESS", "conclusion": ""}
        ])

    def test_auto_review_dispatches_on_ready_pr(self):
        """With auto_review=True, a passing PR triggers dispatch."""
        fake_result = type(
            "R",
            (),
            {
                "success": True,
                "pid": 999,
                "error": None,
                "container_id": "c-test",
                "timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            },
        )()
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = fake_result

        pr_json = self._one_pr_json()
        checks_json = self._all_pass_checks()

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_json, "")
            if "pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks_json, "")
            if "which" in cmd_str or "command -v" in cmd_str:
                return subprocess.CompletedProcess([], 0, "/usr/bin/gh", "")
            return subprocess.CompletedProcess([], 0, "", "")

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            dispatcher=fake_dispatcher,
            wl_shell=wl_shell,
        )
        spec = self._make_spec(auto_review=True)
        result = runner.run(spec)

        assert 99 in result["ready_prs"]
        fake_dispatcher.dispatch.assert_called_once()

    def test_auto_review_false_does_not_dispatch(self):
        """With auto_review=False (opt-out), no dispatch occurs."""
        fake_dispatcher = mock.MagicMock()

        pr_json = self._one_pr_json()
        checks_json = self._all_pass_checks()

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_json, "")
            if "pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks_json, "")
            if "which" in cmd_str or "command -v" in cmd_str:
                return subprocess.CompletedProcess([], 0, "/usr/bin/gh", "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            dispatcher=fake_dispatcher,
        )
        spec = self._make_spec(auto_review=False)
        result = runner.run(spec)

        assert 99 in result["ready_prs"]
        fake_dispatcher.dispatch.assert_not_called()

    def test_auto_review_missing_defaults_to_true(self):
        """Missing auto_review metadata defaults to enabled."""
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = type(
            "R",
            (),
            {
                "success": True,
                "pid": 999,
                "error": None,
                "container_id": "c-test",
                "timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            },
        )()

        pr_json = self._one_pr_json()
        checks_json = self._all_pass_checks()

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_json, "")
            if "pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks_json, "")
            if "which" in cmd_str or "command -v" in cmd_str:
                return subprocess.CompletedProcess([], 0, "/usr/bin/gh", "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            dispatcher=fake_dispatcher,
        )
        # metadata without auto_review key
        spec = type("FakeSpec", (), {"metadata": {"dedup": False}})()
        result = runner.run(spec)

        assert 99 in result["ready_prs"]
        assert result["auto_review_enabled"] is True
        fake_dispatcher.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests for _present_audit_results (Phase 2)
# ---------------------------------------------------------------------------


class TestPresentAuditResults:
    """Verify _present_audit_results builds Discord payloads correctly."""

    def _make_runner(self, notifier=None):
        return PRMonitorRunner(
            run_shell=lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
            command_cwd="/tmp",
            notifier=notifier,
        )

    def _make_audit(self, overall="pass", summary="Looks good.", concerns=None,
                    criteria=None):
        return {
            "overall": overall,
            "summary": summary,
            "concerns": concerns or [],
            "criteria": criteria or [],
        }

    def test_no_notifier_is_noop(self):
        runner = self._make_runner(notifier=None)
        audit = self._make_audit()
        # Should not raise
        runner._present_audit_results(42, "Test PR", "http://pr/42", "SA-X1", audit)

    def test_pass_embed_sent(self):
        notifier = mock.MagicMock()
        runner = self._make_runner(notifier=notifier)
        audit = self._make_audit(
            overall="pass",
            summary="All criteria met.",
            criteria=[
                {"name": "Tests pass", "pass": True, "notes": "100% coverage"},
                {"name": "Docs updated", "pass": True, "notes": ""},
            ],
        )
        runner._present_audit_results(42, "Good PR", "http://pr/42", "SA-X1", audit)

        notifier.notify.assert_called_once()
        call_kwargs = notifier.notify.call_args[1]
        payload = call_kwargs["payload"]

        # Check embed
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "PASS" in embed["title"]
        assert embed["color"] == 0x2ECC71  # green

        # Check buttons
        assert len(payload["components"]) == 2
        approve_btn = payload["components"][0]
        reject_btn = payload["components"][1]
        assert approve_btn["custom_id"] == "pr_review_approve_42"
        assert reject_btn["custom_id"] == "pr_review_reject_42"

    def test_fail_embed_colour(self):
        notifier = mock.MagicMock()
        runner = self._make_runner(notifier=notifier)
        audit = self._make_audit(overall="fail", summary="Failed.")
        runner._present_audit_results(10, "Bad PR", "http://pr/10", "SA-X2", audit)

        payload = notifier.notify.call_args[1]["payload"]
        assert payload["embeds"][0]["color"] == 0xE74C3C  # red

    def test_partial_embed_colour(self):
        notifier = mock.MagicMock()
        runner = self._make_runner(notifier=notifier)
        audit = self._make_audit(overall="partial", summary="Some issues.")
        runner._present_audit_results(11, "OK PR", "http://pr/11", "SA-X3", audit)

        payload = notifier.notify.call_args[1]["payload"]
        assert payload["embeds"][0]["color"] == 0xF39C12  # yellow

    def test_concerns_included(self):
        notifier = mock.MagicMock()
        runner = self._make_runner(notifier=notifier)
        audit = self._make_audit(
            concerns=["Missing error handling", "No migration script"]
        )
        runner._present_audit_results(13, "PR", "http://pr/13", "SA-X4", audit)

        payload = notifier.notify.call_args[1]["payload"]
        embed = payload["embeds"][0]
        concern_fields = [f for f in embed["fields"] if f["name"] == "Concerns"]
        assert len(concern_fields) == 1
        assert "Missing error handling" in concern_fields[0]["value"]

    def test_notifier_exception_does_not_propagate(self):
        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("Discord down")
        runner = self._make_runner(notifier=notifier)
        audit = self._make_audit()
        # Should not raise
        runner._present_audit_results(14, "PR", "http://pr/14", "SA-X5", audit)


# ---------------------------------------------------------------------------
# Unit tests for _check_and_present_audit_results (Phase 2 orchestration)
# ---------------------------------------------------------------------------


class TestCheckAndPresentAuditResults:
    """Verify _check_and_present_audit_results orchestration logic."""

    _DISPATCH_MARKER_PREFIX = "<!-- ampa-pr-audit-dispatch:"
    _RESULT_MARKER = "<!-- ampa-pr-audit-result -->"

    def _make_runner(self, wl_shell=None, notifier=None, dispatcher=None):
        return PRMonitorRunner(
            run_shell=lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
            command_cwd="/tmp",
            notifier=notifier,
            dispatcher=dispatcher,
            wl_shell=wl_shell,
        )

    def test_no_work_item_is_noop(self):
        """If no work item ID extracted, do nothing."""
        runner = self._make_runner()
        pr = {"headRefName": "random-branch", "number": 20}
        # Should not raise
        runner._check_and_present_audit_results("gh", pr, 20, "PR", "")

    def test_no_dispatch_triggers_dispatch(self):
        """If no dispatch marker, trigger a new dispatch."""
        fake_result = type(
            "R", (), {
                "success": True, "pid": 1, "error": None,
                "container_id": "c1",
                "timestamp": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            }
        )()
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = fake_result

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(
            wl_shell=wl_shell, dispatcher=fake_dispatcher
        )
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 21}
        runner._check_and_present_audit_results("gh", pr, 21, "PR", "")

        # Should have triggered dispatch
        fake_dispatcher.dispatch.assert_called_once()

    def test_dispatch_exists_no_result_is_noop(self):
        """Dispatch marker exists but no result yet — silent skip."""
        dispatch_comment = json.dumps({
            "dispatch_state": {
                "pr_number": 22,
                "dispatched_at": "2026-01-01T00:00:00+00:00",
                "container_id": "c1",
                "work_item_id": "SA-TEST123456",
            }
        })
        marker = f"{self._DISPATCH_MARKER_PREFIX}22 -->"

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0,
                    json.dumps({
                        "comments": [{"comment": f"{marker}\n{dispatch_comment}"}]
                    }),
                    "",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = self._make_runner(wl_shell=wl_shell, notifier=notifier)
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 22}
        runner._check_and_present_audit_results("gh", pr, 22, "PR", "")

        notifier.notify.assert_not_called()

    def test_result_found_presents_to_discord(self):
        """Audit result exists — Discord embed with buttons is sent."""
        dispatch_comment = json.dumps({
            "dispatch_state": {
                "pr_number": 23,
                "dispatched_at": "2026-01-01T00:00:00+00:00",
                "container_id": "c1",
                "work_item_id": "SA-TEST123456",
            }
        })
        dispatch_marker = f"{self._DISPATCH_MARKER_PREFIX}23 -->"

        result_payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "summary": "Everything checks out.",
                "concerns": [],
                "criteria": [{"name": "Tests", "pass": True, "notes": "OK"}],
                "pr_number": 23,
                "audited_at": "2026-01-02T00:00:00+00:00",
            }
        })
        result_marker = self._RESULT_MARKER

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0,
                    json.dumps({
                        "comments": [
                            {"comment": f"{dispatch_marker}\n{dispatch_comment}"},
                            {"comment": f"{result_marker}\n{result_payload}"},
                        ]
                    }),
                    "",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = self._make_runner(wl_shell=wl_shell, notifier=notifier)
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 23}
        runner._check_and_present_audit_results(
            "gh", pr, 23, "Audited PR", "http://pr/23"
        )

        notifier.notify.assert_called_once()
        payload = notifier.notify.call_args[1]["payload"]
        assert "PASS" in payload["embeds"][0]["title"]
        assert payload["components"][0]["custom_id"] == "pr_review_approve_23"

    def test_stale_result_triggers_redispatch(self):
        """When PR updatedAt is newer than audit result, trigger re-dispatch."""
        dispatch_comment = json.dumps({
            "dispatch_state": {
                "pr_number": 25,
                "dispatched_at": "2026-01-01T00:00:00+00:00",
                "container_id": "c1",
                "work_item_id": "SA-TEST123456",
            }
        })
        dispatch_marker = f"{self._DISPATCH_MARKER_PREFIX}25 -->"

        stale_result_payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "summary": "Old result",
                "criteria": [],
                "pr_number": 25,
                "audited_at": "2026-01-01T00:00:00+00:00",
            }
        })

        fake_result = type(
            "R", (), {
                "success": True,
                "pid": 2,
                "error": None,
                "container_id": "c2",
                "timestamp": dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc),
            }
        )()
        fake_dispatcher = mock.MagicMock()
        fake_dispatcher.dispatch.return_value = fake_result

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "wl show" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0,
                    json.dumps({
                        "comments": [
                            {"comment": f"{dispatch_marker}\n{dispatch_comment}"},
                            {
                                "comment": (
                                    f"{self._RESULT_MARKER}\n{stale_result_payload}"
                                )
                            },
                        ]
                    }),
                    "",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(wl_shell=wl_shell, dispatcher=fake_dispatcher)
        pr = {
            "headRefName": "feature/SA-TEST123456-foo",
            "number": 25,
            "updatedAt": "2026-01-02T00:00:00+00:00",
        }
        runner._check_and_present_audit_results("gh", pr, 25, "PR", "")

        fake_dispatcher.dispatch.assert_called_once()

    def test_exception_does_not_propagate(self):
        """Internal errors are caught — run continues."""
        def wl_shell(cmd, **kwargs):
            raise RuntimeError("wl crashed")

        runner = self._make_runner(wl_shell=wl_shell)
        pr = {"headRefName": "feature/SA-TEST123456-foo", "number": 24}
        # Should not raise
        runner._check_and_present_audit_results("gh", pr, 24, "PR", "")


# ---------------------------------------------------------------------------
# Phase 4: Merge, reject, cleanup, and review decision tests
# ---------------------------------------------------------------------------


class TestMergePr:
    """Tests for PRMonitorRunner._merge_pr()."""

    def _make_runner(self, run_shell=None):
        return PRMonitorRunner(
            run_shell=run_shell or _make_shell({}),
            command_cwd="/test",
        )

    def test_successful_merge(self):
        """gh pr merge succeeds → returns (True, ...)."""
        shell = _make_shell({"pr merge": {"returncode": 0, "stdout": "merged"}})
        runner = self._make_runner(run_shell=shell)
        ok, note = runner._merge_pr("gh", 42)
        assert ok is True
        assert "42" in note

    def test_merge_failure_nonzero_exit(self):
        """gh pr merge fails → returns (False, ...) with stderr info."""
        shell = _make_shell({
            "pr merge": {"returncode": 1, "stderr": "merge conflict"}
        })
        runner = self._make_runner(run_shell=shell)
        ok, note = runner._merge_pr("gh", 42)
        assert ok is False
        assert "merge conflict" in note

    def test_merge_exception(self):
        """Exception during merge → returns (False, ...) without propagating."""
        shell = _make_shell({"pr merge": OSError("boom")})
        runner = self._make_runner(run_shell=shell)
        ok, note = runner._merge_pr("gh", 42)
        assert ok is False
        assert "boom" in note

    def test_merge_passes_correct_args(self):
        """Verify the correct command is passed to run_shell."""
        calls = []

        def recording_shell(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        runner = self._make_runner(run_shell=recording_shell)
        runner._merge_pr("gh", 99)
        assert len(calls) == 1
        assert calls[0][0] == ["gh", "pr", "merge", "99", "--merge"]
        assert calls[0][1]["cwd"] == "/test"


class TestCloseWorkItem:
    """Tests for PRMonitorRunner._close_work_item()."""

    def _make_runner(self, wl_shell=None):
        return PRMonitorRunner(
            run_shell=_make_shell({}),
            command_cwd="/test",
            wl_shell=wl_shell or _make_shell({}),
        )

    def test_successful_close(self):
        shell = _make_shell({"wl close": {"returncode": 0}})
        runner = self._make_runner(wl_shell=shell)
        assert runner._close_work_item("SA-123", "done") is True

    def test_close_failure(self):
        shell = _make_shell({"wl close": {"returncode": 1, "stderr": "not found"}})
        runner = self._make_runner(wl_shell=shell)
        assert runner._close_work_item("SA-123", "done") is False

    def test_close_exception(self):
        shell = _make_shell({"wl close": RuntimeError("crash")})
        runner = self._make_runner(wl_shell=shell)
        assert runner._close_work_item("SA-123", "done") is False

    def test_close_passes_correct_args(self):
        calls = []

        def recording_shell(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        runner = self._make_runner(wl_shell=recording_shell)
        runner._close_work_item("SA-XYZ", "merged by bob")
        assert len(calls) == 1
        assert calls[0] == [
            "wl", "close", "SA-XYZ",
            "--reason", "merged by bob",
            "--json",
        ]


class TestCleanupBranch:
    """Tests for PRMonitorRunner._cleanup_branch()."""

    def _make_runner(self, run_shell=None):
        return PRMonitorRunner(
            run_shell=run_shell or _make_shell({}),
            command_cwd="/test",
        )

    def test_successful_cleanup(self):
        shell = _make_shell({"git push": {"returncode": 0}})
        runner = self._make_runner(run_shell=shell)
        assert runner._cleanup_branch("gh", "feature/foo") is True

    def test_cleanup_failure(self):
        shell = _make_shell({
            "git push": {"returncode": 1, "stderr": "remote ref does not exist"}
        })
        runner = self._make_runner(run_shell=shell)
        assert runner._cleanup_branch("gh", "feature/foo") is False

    def test_cleanup_exception(self):
        shell = _make_shell({"git push": OSError("network")})
        runner = self._make_runner(run_shell=shell)
        assert runner._cleanup_branch("gh", "feature/foo") is False

    def test_cleanup_passes_correct_args(self):
        calls = []

        def recording_shell(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        runner = self._make_runner(run_shell=recording_shell)
        runner._cleanup_branch("gh", "feature/my-branch")
        assert len(calls) == 1
        assert calls[0] == ["git", "push", "origin", "--delete", "feature/my-branch"]


class TestGetPrBranch:
    """Tests for PRMonitorRunner._get_pr_branch()."""

    def _make_runner(self, run_shell=None):
        return PRMonitorRunner(
            run_shell=run_shell or _make_shell({}),
            command_cwd="/test",
        )

    def test_returns_branch_name(self):
        shell = _make_shell({
            "pr view": {"returncode": 0, "stdout": "feature/SA-123-foo\n"}
        })
        runner = self._make_runner(run_shell=shell)
        assert runner._get_pr_branch("gh", 42) == "feature/SA-123-foo"

    def test_returns_none_on_failure(self):
        shell = _make_shell({"pr view": {"returncode": 1}})
        runner = self._make_runner(run_shell=shell)
        assert runner._get_pr_branch("gh", 42) is None

    def test_returns_none_on_empty_output(self):
        shell = _make_shell({"pr view": {"returncode": 0, "stdout": ""}})
        runner = self._make_runner(run_shell=shell)
        assert runner._get_pr_branch("gh", 42) is None

    def test_returns_none_on_exception(self):
        shell = _make_shell({"pr view": OSError("nope")})
        runner = self._make_runner(run_shell=shell)
        assert runner._get_pr_branch("gh", 42) is None


class TestAddWlComment:
    """Tests for PRMonitorRunner._add_wl_comment()."""

    def _make_runner(self, wl_shell=None):
        return PRMonitorRunner(
            run_shell=_make_shell({}),
            command_cwd="/test",
            wl_shell=wl_shell or _make_shell({}),
        )

    def test_successful_comment(self):
        shell = _make_shell({"wl comment": {"returncode": 0}})
        runner = self._make_runner(wl_shell=shell)
        assert runner._add_wl_comment("SA-123", "all good") is True

    def test_comment_failure(self):
        shell = _make_shell({"wl comment": {"returncode": 1}})
        runner = self._make_runner(wl_shell=shell)
        assert runner._add_wl_comment("SA-123", "all good") is False

    def test_comment_exception(self):
        shell = _make_shell({"wl comment": RuntimeError("crash")})
        runner = self._make_runner(wl_shell=shell)
        assert runner._add_wl_comment("SA-123", "all good") is False


class TestNotifyReviewOutcome:
    """Tests for PRMonitorRunner._notify_review_outcome()."""

    def _make_runner(self, notifier=None):
        return PRMonitorRunner(
            run_shell=_make_shell({}),
            command_cwd="/test",
            notifier=notifier,
        )

    def test_sends_notification(self):
        notifier = mock.MagicMock()
        runner = self._make_runner(notifier=notifier)
        runner._notify_review_outcome(42, "PR Merged", "All done", color=0x2ECC71)
        notifier.notify.assert_called_once()
        payload = notifier.notify.call_args[1]["payload"]
        assert payload["embeds"][0]["title"] == "PR Merged"
        assert payload["embeds"][0]["description"] == "All done"
        assert payload["embeds"][0]["color"] == 0x2ECC71

    def test_no_notifier_is_noop(self):
        runner = self._make_runner(notifier=None)
        # Should not raise
        runner._notify_review_outcome(42, "PR Merged", "All done")

    def test_notifier_exception_does_not_propagate(self):
        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("discord down")
        runner = self._make_runner(notifier=notifier)
        # Should not raise
        runner._notify_review_outcome(42, "PR Merged", "All done")


class TestHandleReviewDecisionApprove:
    """Tests for handle_review_decision() with action=accept."""

    def _make_runner(self, run_shell=None, wl_shell=None, notifier=None):
        return PRMonitorRunner(
            run_shell=run_shell or _make_shell({}),
            command_cwd="/test",
            wl_shell=wl_shell or _make_shell({}),
            notifier=notifier,
        )

    def test_approve_merges_and_returns_merged(self):
        """Successful approval merges PR and returns action=merged."""
        shell = _make_shell({
            "pr merge": {"returncode": 0},
            "pr view": {"returncode": 0, "stdout": "feature/my-branch\n"},
            "git push": {"returncode": 0},
        })
        wl_shell = _make_shell({
            "wl close": {"returncode": 0},
            "wl comment": {"returncode": 0},
        })
        notifier = mock.MagicMock()
        runner = self._make_runner(
            run_shell=shell, wl_shell=wl_shell, notifier=notifier
        )
        result = runner.handle_review_decision(
            action="accept",
            pr_number=42,
            work_item_id="SA-123",
            approved_by="alice",
        )
        assert result["action"] == "merged"
        assert "42" in result["note"]
        assert "alice" in result["note"]
        # Notification sent
        notifier.notify.assert_called()
        payload = notifier.notify.call_args[1]["payload"]
        assert "Merged" in payload["embeds"][0]["title"]

    def test_approve_without_work_item_skips_close(self):
        """When no work_item_id, skip wl close but still merge."""
        wl_calls = []

        def tracking_wl(cmd, **kwargs):
            wl_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        shell = _make_shell({
            "pr merge": {"returncode": 0},
            "pr view": {"returncode": 0, "stdout": "feature/br\n"},
            "git push": {"returncode": 0},
        })
        runner = self._make_runner(run_shell=shell, wl_shell=tracking_wl)
        result = runner.handle_review_decision(
            action="accept", pr_number=42, work_item_id=None, approved_by="bob"
        )
        assert result["action"] == "merged"
        # No wl close or wl comment calls
        for call in wl_calls:
            cmd_str = " ".join(str(c) for c in call)
            assert "wl close" not in cmd_str

    def test_approve_merge_failure_returns_error(self):
        """If merge fails, return action=error and notify."""
        shell = _make_shell({
            "pr merge": {"returncode": 1, "stderr": "conflict"}
        })
        notifier = mock.MagicMock()
        runner = self._make_runner(run_shell=shell, notifier=notifier)
        result = runner.handle_review_decision(
            action="accept", pr_number=42, approved_by="alice"
        )
        assert result["action"] == "error"
        assert "conflict" in result["note"]
        # Error notification sent
        notifier.notify.assert_called()
        payload = notifier.notify.call_args[1]["payload"]
        assert "Failed" in payload["embeds"][0]["title"]

    def test_approve_branch_not_found_skips_cleanup(self):
        """If _get_pr_branch returns None, skip branch cleanup."""
        shell = _make_shell({
            "pr merge": {"returncode": 0},
            "pr view": {"returncode": 1},  # branch lookup fails
        })
        runner = self._make_runner(run_shell=shell)
        result = runner.handle_review_decision(
            action="accept", pr_number=42, approved_by="alice"
        )
        assert result["action"] == "merged"

    def test_approve_action_variants(self):
        """accept, approve, ACCEPT all treated as approve."""
        shell = _make_shell({
            "pr merge": {"returncode": 0},
            "pr view": {"returncode": 1},
        })
        runner = self._make_runner(run_shell=shell)
        for action_str in ("accept", "approve", "ACCEPT", " Accept "):
            result = runner.handle_review_decision(
                action=action_str, pr_number=1, approved_by="x"
            )
            assert result["action"] == "merged", f"Failed for action={action_str!r}"


class TestHandleReviewDecisionReject:
    """Tests for handle_review_decision() with action=decline."""

    def _make_runner(self, run_shell=None, wl_shell=None, notifier=None):
        return PRMonitorRunner(
            run_shell=run_shell or _make_shell({}),
            command_cwd="/test",
            wl_shell=wl_shell or _make_shell({}),
            notifier=notifier,
        )

    def test_reject_posts_comment_and_returns_rejected(self):
        """Decline posts a GH comment and returns action=rejected."""
        gh_calls = []

        def tracking_shell(cmd, **kwargs):
            gh_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        notifier = mock.MagicMock()
        runner = self._make_runner(run_shell=tracking_shell, notifier=notifier)
        result = runner.handle_review_decision(
            action="decline",
            pr_number=42,
            work_item_id="SA-123",
            approved_by="bob",
        )
        assert result["action"] == "rejected"
        assert "42" in result["note"]
        assert "bob" in result["note"]
        # GH comment posted
        assert any(
            "pr" in str(c) and "comment" in str(c) for c in gh_calls
        )
        # Discord notification sent
        notifier.notify.assert_called()

    def test_reject_without_work_item_id(self):
        """Reject works without a work_item_id."""
        runner = self._make_runner()
        result = runner.handle_review_decision(
            action="decline", pr_number=42, approved_by="bob"
        )
        assert result["action"] == "rejected"

    def test_reject_records_wl_comment(self):
        """Reject records a comment on the work item."""
        wl_calls = []

        def tracking_wl(cmd, **kwargs):
            wl_calls.append(
                cmd if isinstance(cmd, str)
                else " ".join(str(c) for c in cmd)
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        runner = self._make_runner(wl_shell=tracking_wl)
        runner.handle_review_decision(
            action="decline",
            pr_number=42,
            work_item_id="SA-456",
            approved_by="carol",
        )
        assert any("wl comment add SA-456" in c for c in wl_calls)

    def test_decline_action_variants(self):
        """decline, reject, DECLINE all treated as reject."""
        runner = self._make_runner()
        for action_str in ("decline", "reject", "DECLINE", " Decline "):
            result = runner.handle_review_decision(
                action=action_str, pr_number=1, approved_by="x"
            )
            assert result["action"] == "rejected", f"Failed for action={action_str!r}"
