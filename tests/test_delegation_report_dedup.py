"""Tests for delegation report deduplication.

Verifies that duplicate delegation report Discord messages are suppressed
when the report content has not changed between scheduler runs.

Covers acceptance criteria from SA-0MLPPFNVO1Y7X717:
  (a) First report is always sent
  (b) Identical consecutive reports are suppressed
  (c) Changed reports are sent
"""

import datetime as dt
import json
import subprocess

from ampa.scheduler_types import (
    CommandRunResult,
    CommandSpec,
    SchedulerConfig,
)
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
from ampa.delegation import _content_hash, _normalize_in_progress_output
from ampa import notifications as notifications_module
from ampa.engine.core import EngineConfig
from ampa.engine.dispatch import DispatchResult


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


# ---------------------------------------------------------------------------
# Unit tests for _content_hash
# ---------------------------------------------------------------------------


def test_content_hash_deterministic():
    assert _content_hash("hello") == _content_hash("hello")


def test_content_hash_differs_for_different_input():
    assert _content_hash("hello") != _content_hash("world")


def test_content_hash_handles_none():
    assert _content_hash(None) == _content_hash("")


# ---------------------------------------------------------------------------
# Unit test for _is_delegation_report_changed (via orchestrator)
# ---------------------------------------------------------------------------


def test_is_delegation_report_changed_first_call(tmp_path):
    """First call should always return True (no previous hash)."""
    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    assert (
        sched._delegation_orchestrator._is_delegation_report_changed(
            "delegation", "some report"
        )
        is True
    )


def test_is_delegation_report_changed_same_content(tmp_path):
    """Same content on second call should return False."""
    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched._delegation_orchestrator._is_delegation_report_changed(
        "delegation", "same report"
    )
    assert (
        sched._delegation_orchestrator._is_delegation_report_changed(
            "delegation", "same report"
        )
        is False
    )


def test_is_delegation_report_changed_different_content(tmp_path):
    """Different content on second call should return True."""
    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched._delegation_orchestrator._is_delegation_report_changed(
        "delegation", "report A"
    )
    assert (
        sched._delegation_orchestrator._is_delegation_report_changed(
            "delegation", "report B"
        )
        is True
    )


def test_hash_persisted_in_state(tmp_path):
    """Verify the hash is stored in the scheduler state dict."""
    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched._delegation_orchestrator._is_delegation_report_changed(
        "delegation", "test content"
    )
    state = sched.store.get_state("delegation")
    assert "last_delegation_report_hash" in state
    assert state["last_delegation_report_hash"] == _content_hash("test content")


# ---------------------------------------------------------------------------
# Integration: pre-dispatch report dedup via start_command
# ---------------------------------------------------------------------------


def _make_in_progress_shell(in_progress_text):
    """Return a fake run_shell that simulates in-progress items."""

    def fake_run_shell(cmd, **kwargs):
        s = cmd.strip()
        if s == "wl in_progress --json":
            payload = {"workItems": [{"id": "SA-1", "title": "Busy"}]}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if s == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=in_progress_text, stderr=""
            )
        if s == "wl next --json":
            payload = {
                "items": [{"id": "SA-999", "title": "Next work", "status": "open"}]
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return fake_run_shell


def test_first_report_always_sent(tmp_path, monkeypatch):
    """AC (a): The very first delegation report should always be sent."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append(
            {"title": title, "body": body, "message_type": message_type}
        )
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    shell = _make_in_progress_shell("Found 1 in_progress:\n- SA-1 Busy item")
    sched = _make_scheduler(shell, tmp_path)
    spec = _delegation_spec()

    sched.start_command(spec)

    # The pre-dispatch report notification should have been sent
    command_calls = [c for c in notify_calls if c["message_type"] == "command"]
    assert len(command_calls) >= 1, "First report should be sent to Discord"


def test_identical_report_suppressed(tmp_path, monkeypatch):
    """AC (b): Identical consecutive reports should be suppressed."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append(
            {"title": title, "body": body, "message_type": message_type}
        )
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    shell = _make_in_progress_shell("Found 1 in_progress:\n- SA-1 Busy item")
    sched = _make_scheduler(shell, tmp_path)
    spec = _delegation_spec()

    # First run: should send
    sched.start_command(spec)
    first_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert first_count >= 1

    # Second run with identical content: should NOT send additional notifications
    sched.start_command(spec)
    second_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert second_count == first_count, (
        f"Duplicate report should be suppressed; "
        f"expected {first_count} but got {second_count}"
    )


def test_changed_report_sent(tmp_path, monkeypatch):
    """AC (c): Report with different content should be sent."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append(
            {"title": title, "body": body, "message_type": message_type}
        )
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    # First run: one in-progress item
    shell1 = _make_in_progress_shell("Found 1 in_progress:\n- SA-1 Busy item")
    sched = _make_scheduler(shell1, tmp_path)
    spec = _delegation_spec()
    sched.start_command(spec)
    first_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert first_count >= 1

    # Now change the in-progress items -> different report content
    def shell2(cmd, **kwargs):
        s = cmd.strip()
        if s == "wl in_progress --json":
            payload = {"workItems": [{"id": "SA-2", "title": "Different task"}]}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if s == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Found 1 in_progress:\n- SA-2 Different task",
                stderr="",
            )
        if s == "wl next --json":
            payload = {
                "items": [{"id": "SA-888", "title": "Another", "status": "open"}]
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched.run_shell = shell2

    sched.start_command(spec)
    second_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert second_count > first_count, "Changed report should be sent to Discord"


# ---------------------------------------------------------------------------
# Integration: idle-no-candidate dedup
# ---------------------------------------------------------------------------


def test_idle_no_candidate_dedup(tmp_path, monkeypatch):
    """Idle 'no candidate' messages should be deduped on consecutive runs."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append({"message_type": message_type})
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    def shell(cmd, **kwargs):
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
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"items": []}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(shell, tmp_path)
    spec = _delegation_spec()

    # First run
    sched.start_command(spec)
    first_count = len([c for c in notify_calls if c["message_type"] == "command"])

    # Second run with same state
    sched.start_command(spec)
    second_count = len([c for c in notify_calls if c["message_type"] == "command"])

    assert second_count == first_count, "Duplicate idle message should be suppressed"


# ---------------------------------------------------------------------------
# Dispatch notifications should NOT be deduped
# ---------------------------------------------------------------------------


def test_dispatch_notification_always_sent(tmp_path, monkeypatch):
    """Dispatch notifications (message_type='dispatch') should not be suppressed."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append(
            {"title": title, "body": body, "message_type": message_type}
        )
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

    def shell(cmd, **kwargs):
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
                    "id": "SA-DISPATCH-1",
                    "title": "Dispatch me",
                    "status": "open",
                    "stage": "idea",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if "wl show" in s and "SA-DISPATCH-1" in s:
            item = {
                "id": "SA-DISPATCH-1",
                "title": "Dispatch me",
                "status": "open",
                "stage": "idea",
                "description": (
                    "This is a work item for dispatch notification testing. "
                    "It contains sufficient context to satisfy the "
                    "requires_work_item_context invariant which needs "
                    "more than 100 characters in the description.\n\n"
                    "Acceptance Criteria:\n"
                    "- [ ] Dispatch notification is sent\n"
                    "- [ ] Notification is not deduped"
                ),
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(shell, tmp_path)

    # Override fallback_mode to None so engine uses natural stage-to-action
    sched.engine._config = EngineConfig(  # type: ignore[union-attr]
        descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
        fallback_mode=None,
    )

    # Mock engine dispatcher to avoid real subprocess spawning
    def fake_dispatch(command, work_item_id):
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            pid=12345,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    monkeypatch.setattr(sched.engine._dispatcher, "dispatch", fake_dispatch)  # type: ignore[union-attr]

    spec = _delegation_spec()
    sched.store.add_command(spec)

    # Run twice; dispatch notification should appear both times.
    # The engine sends dispatch notifications with message_type="engine".
    sched.start_command(spec)
    first_dispatch = len([c for c in notify_calls if c["message_type"] == "engine"])

    sched.start_command(spec)
    second_dispatch = len([c for c in notify_calls if c["message_type"] == "engine"])

    assert first_dispatch >= 1, "Dispatch notification should be sent on first run"
    assert second_dispatch >= 2, (
        "Dispatch notification should always be sent (not deduped)"
    )


# ---------------------------------------------------------------------------
# Tests for idempotency with changing timestamps/summary lines
# ---------------------------------------------------------------------------


def test_normalize_in_progress_output_filters_timestamp_lines():
    """Verify that 'then X minutes later' lines are filtered out."""
    from ampa.delegation import _normalize_in_progress_output
    
    text_with_timestamp = (
        "In Progress\n"
        "- SA-1 Busy item\n"
        "then two minutes later\n"
        "- SA-2 Another item"
    )
    
    normalized = _normalize_in_progress_output(text_with_timestamp)
    
    assert "then two minutes later" not in normalized.lower()
    assert "SA-1" in normalized
    assert "SA-2" in normalized


def test_normalize_in_progress_output_idempotent():
    """Verify that identical reports produce identical normalized output."""
    from ampa.delegation import _normalize_in_progress_output
    
    text1 = (
        "In Progress\n"
        "Eight work items are in-progress, all at Stage: in_review.\n"
        "SA-0MMOLJEAS1H73NO8 — \"test\": priority critical\n"
        "then two minutes later\n"
        "SA-0MMN9YNS41N1B77L — \"another\": priority high"
    )
    
    text2 = (
        "In Progress\n"
        "Eight work items are in-progress, all at Stage: in_review.\n"
        "SA-0MMOLJEAS1H73NO8 — \"test\": priority critical\n"
        "then two minutes later\n"
        "SA-0MMN9YNS41N1B77L — \"another\": priority high"
    )
    
    norm1 = _normalize_in_progress_output(text1)
    norm2 = _normalize_in_progress_output(text2)
    
    assert norm1 == norm2, "Identical inputs should produce identical normalized output"


def test_normalize_in_progress_output_different_content_detected():
    """Verify that different work items produce different normalized output."""
    from ampa.delegation import _normalize_in_progress_output
    
    text1 = "In Progress\n- SA-1 Item one\nthen two minutes later"
    text2 = "In Progress\n- SA-2 Item two\nthen two minutes later"
    
    norm1 = _normalize_in_progress_output(text1)
    norm2 = _normalize_in_progress_output(text2)
    
    assert norm1 != norm2, "Different work items should produce different output"
    assert "SA-1" in norm1 and "SA-2" not in norm1
    assert "SA-2" in norm2 and "SA-1" not in norm2


# ---------------------------------------------------------------------------
# Tests for generic shell command notification dedup (scheduler path)
# ---------------------------------------------------------------------------


def _make_shell_spec(command_id="wl-in_progress"):
    return CommandSpec(
        command_id=command_id,
        command="wl in_progress",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"discord_label": "wl in_progress"},
        title="In Progress",
        command_type="shell",
    )


def test_shell_dedup_hashes_raw_output_not_summary(tmp_path, monkeypatch):
    """Dedup should hash raw command output, not the LLM-summarized version.

    Even when _summarize_for_discord returns different text each call
    (simulating LLM non-determinism), identical raw output should be
    deduped and only one notification sent.
    """
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append({"title": title, "body": body, "message_type": message_type})
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    # Simulate non-deterministic LLM summaries
    summarize_call_count = [0]
    original_summarize = None

    def nondeterministic_summarize(text, max_chars=2000):
        summarize_call_count[0] += 1
        return f"Summary variant {summarize_call_count[0]}: {text[:50]}"

    monkeypatch.setattr(
        "ampa.delegation._summarize_for_discord", nondeterministic_summarize
    )

    raw_output = "Found 1 in_progress:\n- SA-1 Busy item"

    def shell_executor(_spec):
        now = dt.datetime.now(dt.timezone.utc)
        return CommandRunResult(start_ts=now, end_ts=now, exit_code=0, output=raw_output)

    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched.executor = shell_executor
    spec = _make_shell_spec()
    sched.store.add_command(spec)

    # First run: should send
    sched.start_command(spec)
    first_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert first_count == 1, "First notification should be sent"

    # Second run with identical raw output: should NOT send
    sched.start_command(spec)
    second_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert second_count == 1, (
        "Identical raw output should be deduped even if summarizer is non-deterministic"
    )


def test_shell_dedup_sends_when_raw_output_changes(tmp_path, monkeypatch):
    """When raw command output changes, a new notification should be sent."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append({"title": title, "body": body, "message_type": message_type})
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    outputs = ["Found 1 in_progress:\n- SA-1 Item A"]

    def shell_executor(_spec):
        now = dt.datetime.now(dt.timezone.utc)
        return CommandRunResult(
            start_ts=now, end_ts=now, exit_code=0, output=outputs[0]
        )

    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched.executor = shell_executor
    spec = _make_shell_spec()
    sched.store.add_command(spec)

    # First run
    sched.start_command(spec)
    first_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert first_count == 1

    # Change raw output
    outputs[0] = "Found 2 in_progress:\n- SA-1 Item A\n- SA-2 Item B"
    sched.start_command(spec)
    second_count = len([c for c in notify_calls if c["message_type"] == "command"])
    assert second_count == 2, "Changed raw output should trigger a new notification"


def test_shell_summarizer_not_called_when_deduped(tmp_path, monkeypatch):
    """_summarize_for_discord should NOT be called when output is unchanged."""
    notify_calls = []

    def fake_notify(title, body="", message_type="other", *, payload=None):
        notify_calls.append({"title": title, "body": body, "message_type": message_type})
        return 204

    monkeypatch.setattr(notifications_module, "notify", fake_notify)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    summarize_calls = [0]

    def tracking_summarize(text, max_chars=2000):
        summarize_calls[0] += 1
        return text

    monkeypatch.setattr(
        "ampa.delegation._summarize_for_discord", tracking_summarize
    )

    raw_output = "Found 1 in_progress:\n- SA-1 Busy item"

    def shell_executor(_spec):
        now = dt.datetime.now(dt.timezone.utc)
        return CommandRunResult(start_ts=now, end_ts=now, exit_code=0, output=raw_output)

    sched = _make_scheduler(lambda *a, **kw: None, tmp_path)
    sched.executor = shell_executor
    spec = _make_shell_spec()
    sched.store.add_command(spec)

    # First run: summarizer called
    sched.start_command(spec)
    assert summarize_calls[0] == 1

    # Second run: raw output unchanged, summarizer should NOT be called
    sched.start_command(spec)
    assert summarize_calls[0] == 1, (
        "Summarizer should not be called when raw output is unchanged (deduped)"
    )


def test_summarize_for_discord_threshold_is_2000():
    """Verify _summarize_for_discord default threshold is 2000 chars."""
    import inspect
    from ampa.delegation import _summarize_for_discord

    sig = inspect.signature(_summarize_for_discord)
    default = sig.parameters["max_chars"].default
    assert default == 2000, f"Expected default max_chars=2000, got {default}"
