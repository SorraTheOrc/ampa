"""Tests for the auto-delegate scheduler command.

Verifies that:
1. AutoDelegateRunner skips when wl next returns no candidate.
2. AutoDelegateRunner skips when the candidate does not meet stage/priority criteria.
3. AutoDelegateRunner delegates when criteria match.
4. AutoDelegateRunner retries on delegate failure with exponential back-off.
5. AutoDelegateRunner sends a Discord failure notification after all retries exhausted.
6. The scheduler routes command_type='auto-delegate' through AutoDelegateRunner.
7. The auto-delegate command is disabled by default (enabled=false in metadata).
8. The command is auto-registered at scheduler init.
9. When disabled, the scheduler skips execution.
10. Custom eligible_stages and eligible_priorities are respected.
11. wl next query failures are handled gracefully.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.auto_delegate import AutoDelegateRunner, _normalize_candidates, _extract_github_url
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


def _make_auto_delegate_spec(
    command_id: str = "auto-delegate",
    enabled: Any = True,
    eligible_stages: Optional[List[str]] = None,
    eligible_priorities: Optional[List[str]] = None,
    max_retries: int = 3,
    backoff_base: float = 0.0,
) -> CommandSpec:
    metadata: Dict[str, Any] = {
        "enabled": enabled,
        "max_retries": max_retries,
        "retry_backoff_base_seconds": backoff_base,
    }
    if eligible_stages is not None:
        metadata["eligible_stages"] = eligible_stages
    if eligible_priorities is not None:
        metadata["eligible_priorities"] = eligible_priorities
    return CommandSpec(
        command_id=command_id,
        command="echo auto-delegate",
        requires_llm=False,
        frequency_minutes=30,
        priority=0,
        metadata=metadata,
        title="Auto Delegate",
        max_runtime_minutes=5,
        command_type="auto-delegate",
    )


def _noop_executor(spec: CommandSpec) -> RunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return RunResult(start_ts=start, end_ts=end, exit_code=0)


def _make_shell(responses: Dict[str, Any]):
    """Build a run_shell stub that maps command prefixes to responses.

    Each value in *responses* is either:
    - A dict with optional keys ``returncode``, ``stdout``, ``stderr``
    - An exception type/instance to raise
    """

    def _shell(cmd, **kwargs):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
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
        run_shell=run_shell or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
        engine=engine_mock,
    )
    return sched


# ---------------------------------------------------------------------------
# Unit tests for AutoDelegateRunner
# ---------------------------------------------------------------------------


class TestAutoDelegateRunnerNoCandidates:
    """wl next returns no candidates."""

    def test_returns_no_candidate_action(self):
        run_shell = _make_shell({"wl next": {"returncode": 0, "stdout": "{}"}})
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "no_candidate"
        assert result["work_item_id"] is None

    def test_empty_stdout_returns_no_candidate(self):
        run_shell = _make_shell({"wl next": {"returncode": 0, "stdout": ""}})
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "no_candidate"

    def test_query_failure_returns_query_failed(self):
        run_shell = _make_shell({"wl next": {"returncode": 1, "stderr": "error"}})
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "query_failed"

    def test_invalid_json_returns_query_failed(self):
        run_shell = _make_shell({"wl next": {"returncode": 0, "stdout": "not-json"}})
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "query_failed"

    def test_exception_in_run_shell_returns_query_failed(self):
        def bad_shell(*args, **kwargs):
            raise RuntimeError("boom")

        runner = AutoDelegateRunner(
            run_shell=bad_shell,
            command_cwd="/tmp",
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "query_failed"


class TestAutoDelegateRunnerSkipped:
    """Candidates that do not meet stage/priority criteria are skipped."""

    def _candidate_json(self, stage: str, priority: str, wid: str = "WI-1") -> str:
        return json.dumps({"id": wid, "stage": stage, "priority": priority, "title": "Test"})

    def test_wrong_stage_skipped(self):
        run_shell = _make_shell(
            {"wl next": {"returncode": 0, "stdout": self._candidate_json("intake", "high")}}
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "skipped"
        assert result["work_item_id"] == "WI-1"

    def test_wrong_priority_skipped(self):
        run_shell = _make_shell(
            {"wl next": {"returncode": 0, "stdout": self._candidate_json("in_review", "low")}}
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "skipped"

    def test_custom_eligible_stages_respected(self):
        run_shell = _make_shell(
            {"wl next": {"returncode": 0, "stdout": self._candidate_json("plan_complete", "high")}}
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec(eligible_stages=["prd_complete"])
        result = runner.run(spec)
        assert result["action"] == "skipped"

    def test_custom_eligible_priorities_respected(self):
        run_shell = _make_shell(
            {"wl next": {"returncode": 0, "stdout": self._candidate_json("in_review", "critical")}}
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec(eligible_priorities=["high"])
        result = runner.run(spec)
        assert result["action"] == "skipped"


class TestAutoDelegateRunnerDelegated:
    """Happy path: candidate meets criteria and delegation succeeds."""

    def _candidate_json(self, stage: str = "in_review", priority: str = "high", wid: str = "WI-42") -> str:
        return json.dumps({"id": wid, "stage": stage, "priority": priority, "title": "Do the thing"})

    def test_delegates_when_criteria_match(self):
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": self._candidate_json()},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "delegated"
        assert result["work_item_id"] == "WI-42"
        assert result["retries"] == 0

    def test_delegates_with_critical_priority(self):
        candidate = json.dumps({"id": "WI-99", "stage": "in_review", "priority": "critical", "title": "Critical"})
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": candidate},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "delegated"
        assert result["work_item_id"] == "WI-99"

    def test_candidate_in_list_payload(self):
        payload = json.dumps([
            {"id": "WI-1", "stage": "in_review", "priority": "high", "title": "First"}
        ])
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": payload},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "delegated"

    def test_candidate_in_items_key(self):
        payload = json.dumps({"items": [
            {"id": "WI-55", "stage": "in_review", "priority": "high", "title": "From items"}
        ]})
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": payload},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(run_shell=run_shell, command_cwd="/tmp", sleep_fn=lambda _: None)
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "delegated"
        assert result["work_item_id"] == "WI-55"

    def test_success_notification_sent(self):
        """Notifier is called on successful delegation with work item ID, stage, priority."""
        notifier = mock.MagicMock()
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": self._candidate_json()},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)

        assert result["action"] == "delegated"
        notifier.notify.assert_called_once()
        call_kwargs = notifier.notify.call_args.kwargs
        assert call_kwargs.get("message_type") == "completion"
        body = call_kwargs.get("body", "")
        assert "WI-42" in body
        assert "in_review" in body
        assert "high" in body

    def test_success_notification_includes_github_url(self):
        """When wl gh delegate outputs a GitHub URL it is included in the notification."""
        notifier = mock.MagicMock()
        gh_url = "https://github.com/SorraTheOrc/SorraAgents/issues/123"
        run_shell = _make_shell(
            {
                "wl next": {
                    "returncode": 0,
                    "stdout": self._candidate_json(stage="in_review", priority="high", wid="WI-99"),
                },
                "wl gh delegate": {"returncode": 0, "stdout": f"Issue created: {gh_url}"},
            }
        )
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)

        assert result["action"] == "delegated"
        assert result.get("github_url") == gh_url
        notifier.notify.assert_called_once()
        call_kwargs = notifier.notify.call_args.kwargs
        assert call_kwargs.get("message_type") == "completion"
        assert "Auto-delegate succeeded" in call_kwargs.get("title", "")
        body = call_kwargs.get("body", "")
        assert gh_url in body
        assert "WI-99" in body
        assert "in_review" in body
        assert "high" in body

    def test_success_notification_without_github_url(self):
        """When delegate output has no URL, notification still succeeds without URL."""
        notifier = mock.MagicMock()
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": self._candidate_json()},
                "wl gh delegate": {"returncode": 0, "stdout": "Delegated successfully."},
            }
        )
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)

        assert result["action"] == "delegated"
        assert result.get("github_url") is None
        notifier.notify.assert_called_once()
        body = notifier.notify.call_args.kwargs.get("body", "")
        assert "WI-42" in body
        assert "Destination: (GitHub URL pending)" in body

    def test_success_notification_failure_does_not_propagate(self):
        """An exception in the success notifier must not escape run()."""
        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("discord down")
        run_shell = _make_shell(
            {
                "wl next": {"returncode": 0, "stdout": self._candidate_json()},
                "wl gh delegate": {"returncode": 0},
            }
        )
        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec()
        result = runner.run(spec)
        assert result["action"] == "delegated"


class TestAutoDelegateRunnerRetry:
    """Retry and back-off behaviour on wl gh delegate failure."""

    def _candidate_json(self, wid: str = "WI-7") -> str:
        return json.dumps({"id": wid, "stage": "in_review", "priority": "high", "title": "Retry me"})

    def test_retries_on_failure_and_succeeds(self):
        call_count = {"n": 0}
        delays: list = []

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "wl next" in cmd_str:
                return subprocess.CompletedProcess([], 0, self._candidate_json(), "")
            if "wl gh delegate" in cmd_str:
                call_count["n"] += 1
                # Fail first 2 attempts, succeed on 3rd
                if call_count["n"] < 3:
                    return subprocess.CompletedProcess([], 1, "", "temporary error")
                return subprocess.CompletedProcess([], 0, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            sleep_fn=delays.append,
        )
        spec = _make_auto_delegate_spec(max_retries=3, backoff_base=1.0)
        result = runner.run(spec)

        assert result["action"] == "delegated"
        assert result["retries"] == 2
        assert call_count["n"] == 3
        # Back-off delays: attempt 1→0s, attempt 2→1s (base*2^0), attempt 3→2s (base*2^1)
        assert delays == [1.0, 2.0]

    def test_all_retries_exhausted_sends_notification(self):
        notifier = mock.MagicMock()

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "wl next" in cmd_str:
                return subprocess.CompletedProcess([], 0, self._candidate_json(), "")
            return subprocess.CompletedProcess([], 1, "", "persistent error")

        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec(max_retries=2, backoff_base=0.0)
        result = runner.run(spec)

        assert result["action"] == "delegate_failed"
        assert result["retries"] == 2
        notifier.notify.assert_called_once()
        call_kwargs = notifier.notify.call_args
        body = call_kwargs.kwargs.get("body", "") or ""
        assert "WI-7" in body

    def test_failure_notification_not_sent_when_notifier_raises(self):
        """Notification failure should not propagate exceptions."""
        def bad_notifier(**kwargs):
            raise RuntimeError("discord down")

        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("discord down")

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "wl next" in cmd_str:
                return subprocess.CompletedProcess([], 0, self._candidate_json(), "")
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = AutoDelegateRunner(
            run_shell=run_shell,
            command_cwd="/tmp",
            notifier=notifier,
            sleep_fn=lambda _: None,
        )
        spec = _make_auto_delegate_spec(max_retries=1, backoff_base=0.0)
        # Should not raise despite notification failure
        result = runner.run(spec)
        assert result["action"] == "delegate_failed"


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------


class TestSchedulerAutoDelegate:
    """The scheduler correctly routes auto-delegate command types."""

    def _make_spec(self, enabled: Any = True) -> CommandSpec:
        return _make_auto_delegate_spec(enabled=enabled)

    def test_scheduler_runs_auto_delegate(self):
        """When enabled and candidate qualifies, scheduler delegates."""
        candidate_json = json.dumps(
            {"id": "WI-10", "stage": "in_review", "priority": "high", "title": "Test"}
        )

        calls = {"delegate": 0}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "wl next" in cmd_str:
                return subprocess.CompletedProcess([], 0, candidate_json, "")
            if "wl gh delegate" in cmd_str:
                calls["delegate"] += 1
                return subprocess.CompletedProcess([], 0, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        sched = _make_scheduler(run_shell=run_shell)

        notifier_mock = mock.MagicMock()
        with mock.patch("ampa.scheduler.notifications_module", notifier_mock):
            sched.start_command(self._make_spec(enabled=True))

        assert calls["delegate"] == 1

    def test_scheduler_skips_when_disabled(self):
        """When metadata.enabled is False the scheduler skips delegation."""
        calls = {"delegate": 0}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if "wl gh delegate" in cmd_str:
                calls["delegate"] += 1
            return subprocess.CompletedProcess([], 0, "", "")

        sched = _make_scheduler(run_shell=run_shell)
        with mock.patch("ampa.scheduler.notifications_module"):
            sched.start_command(self._make_spec(enabled=False))

        assert calls["delegate"] == 0

    def test_scheduler_exception_does_not_propagate(self):
        """An exception in the runner should not escape start_command."""

        def bad_shell(cmd, **kwargs):
            raise RuntimeError("everything is broken")

        sched = _make_scheduler(run_shell=bad_shell)
        with mock.patch("ampa.scheduler.notifications_module"):
            run = sched.start_command(self._make_spec(enabled=True))

        # run should be a RunResult — no exception raised
        assert run is not None

    def test_auto_delegate_auto_registered(self):
        """Scheduler init auto-registers the auto-delegate command."""
        sched = _make_scheduler()
        cmd_ids = [c.command_id for c in sched.store.list_commands()]
        assert "auto-delegate" in cmd_ids

    def test_auto_registered_command_disabled_by_default(self):
        """Auto-registered auto-delegate command is disabled by default."""
        sched = _make_scheduler()
        cmd = sched.store.get_command("auto-delegate")
        assert cmd is not None
        enabled = cmd.metadata.get("enabled")
        assert enabled is False or enabled == "false" or enabled == 0


# ---------------------------------------------------------------------------
# Unit tests for _normalize_candidates
# ---------------------------------------------------------------------------


class TestNormalizeCandidates:
    def test_list_payload(self):
        items = [{"id": "1", "stage": "in_review"}]
        assert _normalize_candidates(items) == items

    def test_dict_with_items_key(self):
        payload = {"items": [{"id": "2"}]}
        assert _normalize_candidates(payload) == [{"id": "2"}]

    def test_dict_with_candidates_key(self):
        payload = {"candidates": [{"id": "3"}]}
        assert _normalize_candidates(payload) == [{"id": "3"}]

    def test_dict_with_id_key_treated_as_single_item(self):
        payload = {"id": "4", "stage": "in_review"}
        assert _normalize_candidates(payload) == [payload]

    def test_empty_dict(self):
        assert _normalize_candidates({}) == []

    def test_none_returns_empty(self):
        assert _normalize_candidates(None) == []

    def test_non_dict_non_list_returns_empty(self):
        assert _normalize_candidates("string") == []
        assert _normalize_candidates(42) == []


class TestExtractGithubUrl:
    def test_extracts_url_from_text(self):
        text = "Issue created: https://github.com/owner/repo/issues/42"
        assert _extract_github_url(text) == "https://github.com/owner/repo/issues/42"

    def test_returns_none_when_no_url(self):
        assert _extract_github_url("Delegated successfully.") is None

    def test_returns_none_for_empty_string(self):
        assert _extract_github_url("") is None

    def test_returns_none_for_none(self):
        assert _extract_github_url(None) is None

    def test_returns_first_url_when_multiple(self):
        text = "See https://github.com/a/b/issues/1 and https://github.com/c/d/issues/2"
        assert _extract_github_url(text) == "https://github.com/a/b/issues/1"
