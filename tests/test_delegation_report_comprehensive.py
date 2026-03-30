"""Comprehensive tests for delegation report fixes.

Covers acceptance criteria from SA-0MLZSKAZ30YBQ268:

Unit tests:
  - _format_in_progress_items(): empty, 'No in-progress work items found',
    error messages, single item, multiple items, mixed content with tree chars
  - _build_dry_run_report(): idle (no items, no candidates), idle with
    candidates, busy with real in-progress items, skip reasons displayed

Integration tests:
  - Delegation flow produces correct Discord report when agents idle with
    no candidates
  - Delegation flow includes skip reason when invariant fails
  - Existing test suites pass without modification
"""

import datetime as dt
import json
import subprocess
import types

import pytest

from ampa.delegation import (
    _build_delegation_report,
    _build_dry_run_report,
    _format_in_progress_items,
    DelegationOrchestrator,
)
from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa import notifications as notifications_module
from ampa.engine.core import EngineConfig
from ampa.engine.dispatch import DispatchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "dispatches": [],
        }

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
        now_dt = dt.datetime.now(dt.timezone.utc)
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


# ===================================================================
# Unit tests: _format_in_progress_items edge cases
# ===================================================================


class TestFormatInProgressEdgeCases:
    """Additional edge cases for _format_in_progress_items beyond Task 1 tests."""

    def test_tree_chars_pipe_prefix(self):
        """Lines prefixed with │ (vertical pipe) tree character."""
        text = "│ - SA-PIPE1 Piped item"
        result = _format_in_progress_items(text)
        assert len(result) == 1
        assert "SA-PIPE1" in result[0]

    def test_tree_chars_mixed_unicode(self):
        """Mixed tree characters ├└│ with items."""
        text = (
            "├── - SA-TREE1 First tree item\n"
            "│   └── - SA-TREE2 Nested item\n"
            "└── - SA-TREE3 Last tree item\n"
        )
        result = _format_in_progress_items(text)
        assert len(result) == 3
        ids = [r for r in result]
        assert any("SA-TREE1" in r for r in ids)
        assert any("SA-TREE2" in r for r in ids)
        assert any("SA-TREE3" in r for r in ids)

    def test_only_header_line_no_items(self):
        """Output with only a header like 'In-progress work items:' and no SA- lines."""
        text = "In-progress work items:\n(empty)\n"
        assert _format_in_progress_items(text) == []

    def test_sa_prefix_in_non_item_context(self):
        """SA- appearing in a non-item context (no leading dash) is ignored."""
        text = "Reference: SA-NOTANITEM is mentioned in the docs."
        assert _format_in_progress_items(text) == []

    def test_multiple_sa_items_with_interleaved_noise(self):
        """Multiple SA- items with non-item lines interleaved."""
        text = (
            "Header:\n"
            "- SA-A1 Alpha task\n"
            "  Some description text\n"
            "  More context\n"
            "- SA-B2 Beta task\n"
            "Footer text\n"
            "- SA-C3 Gamma task\n"
        )
        result = _format_in_progress_items(text)
        assert len(result) == 3
        assert "SA-A1" in result[0]
        assert "SA-B2" in result[1]
        assert "SA-C3" in result[2]

    def test_wl_error_output(self):
        """wl in_progress returning an error message returns empty list."""
        text = "Error: failed to connect to database\nretry in 30s"
        assert _format_in_progress_items(text) == []

    def test_json_output_not_parsed_as_items(self):
        """JSON output (e.g. when --json flag is used) is not treated as items."""
        text = '{"workItems": [{"id": "SA-JSON1", "title": "Task"}]}'
        # Contains "SA-" but not in "- SA-" format
        assert _format_in_progress_items(text) == []

    def test_item_with_status_and_priority_metadata(self):
        """Item lines with metadata (status, priority) are correctly extracted."""
        text = "- SA-META1 Important task (status: in-progress, priority: critical)"
        result = _format_in_progress_items(text)
        assert len(result) == 1
        assert "SA-META1" in result[0]
        assert "critical" in result[0]


# ===================================================================
# Unit tests: _build_dry_run_report comprehensive
# ===================================================================


class TestBuildDryRunReportComprehensive:
    """Comprehensive unit tests for _build_dry_run_report."""

    def test_idle_no_items_no_candidates(self):
        """Idle: no in-progress items, no candidates."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" not in report
        assert "AMPA Delegation" in report
        assert "In-progress items:" in report
        assert "- (none)" in report
        assert "Candidates:" in report
        assert "Top candidate:" in report
        assert "idle" in report.lower()

    def test_idle_with_candidates_and_top(self):
        """Idle: no in-progress items, candidates present with top selected."""
        candidates = [
            {
                "id": "SA-C1",
                "title": "First task",
                "status": "open",
                "priority": "high",
            },
            {
                "id": "SA-C2",
                "title": "Second task",
                "status": "open",
                "priority": "medium",
            },
        ]
        report = _build_dry_run_report(
            in_progress_output="No in-progress work items found",
            candidates=candidates,
            top_candidate=candidates[0],
        )
        assert "Agents are currently busy" not in report
        assert "SA-C1" in report
        assert "SA-C2" in report
        assert "First task" in report
        assert "Second task" in report
        assert "selected by wl next" in report

    def test_idle_with_candidates_no_top(self):
        """Idle: candidates exist but none selected as top."""
        candidates = [
            {"id": "SA-NT1", "title": "No top", "status": "open"},
        ]
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=candidates,
            top_candidate=None,
        )
        assert "SA-NT1" in report
        assert "no candidates returned by wl next" in report

    def test_busy_with_single_item(self):
        """Busy: single in-progress item."""
        report = _build_dry_run_report(
            in_progress_output="- SA-BUSY1 Working hard",
            candidates=[{"id": "SA-C1", "title": "Ignored"}],
            top_candidate={"id": "SA-C1", "title": "Ignored"},
        )
        assert "Agents are currently busy with:" in report
        assert "SA-BUSY1" in report
        # Candidates should NOT appear in busy format
        assert "Candidates:" not in report

    def test_busy_with_multiple_items(self):
        """Busy: multiple in-progress items, all listed."""
        text = "- SA-B1 Task one\n- SA-B2 Task two\n- SA-B3 Task three"
        report = _build_dry_run_report(
            in_progress_output=text,
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy with:" in report
        assert "SA-B1" in report
        assert "SA-B2" in report
        assert "SA-B3" in report

    def test_skip_reasons_single(self):
        """Skip reason: single invariant failure displayed."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            skip_reasons=["invariant requires_acceptance_criteria failed"],
        )
        assert "Delegation skip reasons:" in report
        assert "requires_acceptance_criteria" in report

    def test_skip_reasons_multiple(self):
        """Skip reasons: multiple reasons all displayed."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            skip_reasons=[
                "invariant requires_acceptance_criteria failed",
                "invariant requires_work_item_context failed",
            ],
        )
        assert "Delegation skip reasons:" in report
        assert "requires_acceptance_criteria" in report
        assert "requires_work_item_context" in report

    def test_rejections_single(self):
        """Rejection: single rejected candidate with reason."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[{"id": "SA-R1", "title": "Rejected item", "status": "open"}],
            top_candidate=None,
            rejections=[
                {
                    "id": "SA-R1",
                    "title": "Rejected item",
                    "reason": "stage 'done' is not delegatable",
                },
            ],
        )
        assert "Rejected candidates:" in report
        assert "SA-R1" in report
        assert "stage 'done' is not delegatable" in report

    def test_rejections_multiple(self):
        """Rejections: multiple rejected candidates all listed."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
            rejections=[
                {"id": "SA-R1", "title": "Task A", "reason": "closed stage"},
                {"id": "SA-R2", "title": "Task B", "reason": "do-not-delegate tag"},
                {"id": "SA-R3", "title": "Task C", "reason": "missing description"},
            ],
        )
        assert "SA-R1" in report
        assert "SA-R2" in report
        assert "SA-R3" in report
        assert "closed stage" in report
        assert "do-not-delegate tag" in report
        assert "missing description" in report

    def test_combined_skip_and_rejections(self):
        """Both skip_reasons and rejections appear together."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[{"id": "SA-COMBO", "title": "Combo", "status": "open"}],
            top_candidate=None,
            skip_reasons=["invariant check_assignee failed"],
            rejections=[
                {"id": "SA-COMBO", "title": "Combo", "reason": "no description"},
            ],
        )
        assert "Delegation skip reasons:" in report
        assert "check_assignee" in report
        assert "Rejected candidates:" in report
        assert "no description" in report

    def test_busy_suppresses_skip_and_rejections(self):
        """When busy, skip_reasons and rejections are not shown."""
        report = _build_dry_run_report(
            in_progress_output="- SA-BUSY Working on stuff",
            candidates=[],
            top_candidate=None,
            skip_reasons=["invariant failed"],
            rejections=[{"id": "SA-X", "title": "X", "reason": "rejected"}],
        )
        assert "Agents are currently busy with:" in report
        assert "Delegation skip reasons:" not in report
        assert "Rejected candidates:" not in report

    def test_idle_summary_when_no_candidates_no_top(self):
        """Idle with no candidates and no top produces summary line."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
        )
        assert "delegation is idle" in report.lower()

    def test_no_idle_summary_when_candidates_present(self):
        """When candidates exist, the idle summary line is not added."""
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[{"id": "SA-1", "title": "Task", "status": "open"}],
            top_candidate={"id": "SA-1", "title": "Task", "status": "open"},
        )
        assert "delegation is idle" not in report.lower()


# ===================================================================
# Unit tests: _build_delegation_report parity
# ===================================================================


class TestBuildDelegationReportParity:
    """Verify _build_delegation_report delegates to _build_dry_run_report."""

    def test_delegation_report_matches_dry_run_report(self):
        """Output of _build_delegation_report is identical to _build_dry_run_report."""
        kwargs = {
            "in_progress_output": "",
            "candidates": [{"id": "SA-1", "title": "Task"}],
            "top_candidate": {"id": "SA-1", "title": "Task"},
            "skip_reasons": ["test reason"],
            "rejections": [{"id": "SA-2", "title": "Rej", "reason": "why"}],
        }
        assert _build_dry_run_report(**kwargs) == _build_delegation_report(**kwargs)

    def test_delegation_report_busy_parity(self):
        """Busy path output is identical between both report functions."""
        kwargs = {
            "in_progress_output": "- SA-BUSY Doing work",
            "candidates": [],
            "top_candidate": None,
        }
        assert _build_dry_run_report(**kwargs) == _build_delegation_report(**kwargs)


# ===================================================================
# Integration: idle flow with no candidates produces correct report
# ===================================================================


class TestIntegrationIdleNoCandidates:
    """Integration: delegation flow when agents are idle with no candidates.

    There are two no-candidate paths:
    1. wl next returns zero items → CandidateSelector adds a global rejection
       → _inspect_idle_delegation returns status="error" → execute prints
       "There is no candidate to delegate."
    2. wl next returns items but all are rejected (bad stage, do-not-delegate)
       → CandidateSelector returns selected=None with no global rejections
       → _inspect_idle_delegation returns status="idle_no_candidate"
       → execute prints "Delegation idle: no candidates returned"

    Both paths must NOT say "Agents are currently busy".
    """

    def test_zero_candidates_console_output(self, tmp_path, capsys, monkeypatch):
        """When wl next returns zero items, output does NOT say busy."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="No in-progress work items found",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"items": []}),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        assert "Agents are currently busy" not in out
        assert "no candidate" in out.lower()

    def test_all_rejected_console_output(self, tmp_path, capsys, monkeypatch):
        """When all candidates are rejected, output says 'Delegation idle:
        no candidates returned'."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="No in-progress work items found",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                # Return candidates that will all be rejected (bad stage)
                payload = {
                    "items": [
                        {
                            "id": "SA-BAD1",
                            "title": "Closed item",
                            "stage": "closed",
                            "status": "completed",
                        },
                        {
                            "id": "SA-BAD2",
                            "title": "Also closed",
                            "stage": "done",
                            "status": "completed",
                        },
                    ]
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        assert "Agents are currently busy" not in out
        assert "Delegation idle: no candidates returned" in out

    def test_all_rejected_discord_notification(self, tmp_path, monkeypatch):
        """Discord notification sent with idle content when all candidates rejected."""
        notify_calls = []

        def fake_notify(title, body="", message_type="other", *, payload=None):
            notify_calls.append({"title": title, "body": body, "type": message_type})
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="No in-progress work items found",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                payload = {
                    "items": [
                        {
                            "id": "SA-REJ1",
                            "title": "Rejected",
                            "stage": "closed",
                            "status": "completed",
                        },
                    ]
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())

        command_calls = [c for c in notify_calls if c["type"] == "command"]
        assert len(command_calls) >= 1, "Should send at least one notification"
        # The notification body should reflect idle state, not busy
        for call in command_calls:
            assert "Agents are currently busy" not in call["body"]

    def test_no_items_message_not_busy_in_report(self, tmp_path, monkeypatch):
        """The pre-dispatch report uses idle format when wl in_progress says
        'No in-progress work items found'."""
        notify_calls = []

        def fake_notify(title, body="", message_type="other", *, payload=None):
            notify_calls.append({"title": title, "body": body, "type": message_type})
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="No in-progress work items found",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                payload = {
                    "items": [
                        {
                            "id": "SA-X1",
                            "title": "Bad stage",
                            "stage": "closed",
                            "status": "completed",
                        },
                    ]
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())

        command_calls = [c for c in notify_calls if c["type"] == "command"]
        assert len(command_calls) >= 1
        body = command_calls[0]["body"]
        assert "Agents are currently busy" not in body


# ===================================================================
# Integration: skip reason surfaced when invariant fails
# ===================================================================


class TestIntegrationInvariantFailure:
    """Integration: delegation flow includes skip reason when invariant fails.

    When a candidate exists but the engine rejects it due to an invariant
    failure (e.g. requires_work_item_context), the console output should
    surface the actual reason rather than saying 'Agents are currently busy'.
    """

    def test_invariant_failure_console_output(self, tmp_path, capsys, monkeypatch):
        """Console output includes 'blocked' or 'skipped' reason when
        invariant fails."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            if "wl next" in s and "--json" in s:
                # Return a candidate that will fail invariant check
                payload = {
                    "workItem": {
                        "id": "SA-INVARIANT1",
                        "title": "Missing criteria",
                        "status": "open",
                        "stage": "idea",
                    }
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            if "wl show" in s and "SA-INVARIANT1" in s:
                # Return item with no description / no acceptance criteria
                # so requires_work_item_context invariant fails
                item = {
                    "id": "SA-INVARIANT1",
                    "title": "Missing criteria",
                    "status": "open",
                    "stage": "idea",
                    "description": "",  # Too short -> invariant fails
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(item),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)

        # Override fallback_mode to None so engine uses natural stage-to-action
        sched.engine._config = EngineConfig(  # type: ignore[union-attr]
            descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
            fallback_mode=None,
        )

        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        # The output should NOT contain "Agents are currently busy"
        assert "Agents are currently busy" not in out
        # The output should contain a descriptive reason
        # (could be 'blocked', 'skipped', or the specific invariant name)
        assert (
            "blocked" in out.lower()
            or "skipped" in out.lower()
            or "idle" in out.lower()
        ), f"Expected descriptive reason in output, got: {out!r}"

    def test_rejected_candidate_console_output(self, tmp_path, capsys, monkeypatch):
        """Console output includes 'Delegation idle: no candidates returned'
        when all candidates are rejected (e.g. bad stage)."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            if "wl next" in s and "--json" in s:
                # Return candidates that will be rejected
                payload = {
                    "items": [
                        {
                            "id": "SA-REJECT1",
                            "title": "Closed item",
                            "stage": "closed",
                            "status": "completed",
                        },
                    ]
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        # Should not say agents are busy
        assert "Agents are currently busy" not in out
        assert "Delegation idle: no candidates returned" in out

    def test_invariant_failure_discord_has_detail(self, tmp_path, monkeypatch):
        """Discord notification includes detail, not 'busy', when invariant fails."""
        notify_calls = []

        def fake_notify(title, body="", message_type="other", *, payload=None):
            notify_calls.append({"title": title, "body": body, "type": message_type})
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            if "wl next" in s and "--json" in s:
                payload = {
                    "workItem": {
                        "id": "SA-INVFAIL2",
                        "title": "No context",
                        "status": "open",
                        "stage": "idea",
                    }
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            if "wl show" in s and "SA-INVFAIL2" in s:
                item = {
                    "id": "SA-INVFAIL2",
                    "title": "No context",
                    "status": "open",
                    "stage": "idea",
                    "description": "Short",  # Too short for invariant
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(item),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)

        sched.engine._config = EngineConfig(  # type: ignore[union-attr]
            descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
            fallback_mode=None,
        )

        sched.start_command(_delegation_spec())

        # At least one command-type notification should have been sent
        command_calls = [c for c in notify_calls if c["type"] == "command"]
        assert len(command_calls) >= 1, "At least one Discord notification expected"
        # No notification should say "Agents are currently busy" when agents are idle
        for call in command_calls:
            assert "Agents are currently busy" not in call["body"]


# ===================================================================
# Integration: original busy format still works
# ===================================================================


class TestIntegrationBusyFormatPreserved:
    """Regression: busy format is still used when agents have real in-progress work."""

    def test_busy_format_with_real_items(self, tmp_path, capsys, monkeypatch):
        """When agents have in-progress work, the report says 'busy'."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(
                        {"workItems": [{"id": "SA-REAL1", "title": "Real work"}]}
                    ),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="- SA-REAL1 Real work (status: in-progress)",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                payload = {
                    "items": [{"id": "SA-WAIT1", "title": "Waiting", "status": "open"}]
                }
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        assert (
            "no new work will be delegated" in out.lower()
            or "in progress" in out.lower()
        )

    def test_no_items_message_not_treated_as_busy(self, tmp_path, capsys, monkeypatch):
        """The 'No in-progress work items found' message must NOT produce a
        busy report — this was the original bug."""

        def fake_notify(title, body="", message_type="other", *, payload=None):
            return True

        fake_mod = types.SimpleNamespace(notify=fake_notify)
        import ampa.scheduler as schedmod

        monkeypatch.setattr(schedmod, "notifications_module", fake_mod)
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

        def fake_run_shell(cmd, **kwargs):
            s = cmd.strip()
            if s == "wl in_progress --json":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"workItems": []}),
                    stderr="",
                )
            if s == "wl in_progress":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="No in-progress work items found",
                    stderr="",
                )
            if "wl next" in s and "--json" in s:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"items": []}),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        sched = _make_scheduler(fake_run_shell, tmp_path)
        sched.start_command(_delegation_spec())
        out = capsys.readouterr().out

        # This is the original bug — should NOT say "busy"
        assert "Agents are currently busy" not in out
