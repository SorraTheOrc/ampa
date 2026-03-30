"""Unit tests for the audit poller protocol, result types, query, cooldown,
and selection logic.

Work items: SA-0MM2FCXG11VU3CV3, SA-0MM2FD8O70OBNOI6, SA-0MM2FDM751UO1844,
            SA-0MM2FDVGU1XMNPIB
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from typing import Any, Dict, Optional

from ampa.audit_poller import (
    AuditHandoffHandler,
    PollerOutcome,
    PollerResult,
    _filter_by_cooldown,
    _query_candidates,
    _select_candidate,
    poll_and_handoff,
)


# ---------------------------------------------------------------------------
# PollerOutcome
# ---------------------------------------------------------------------------


class TestPollerOutcome:
    def test_enum_members(self) -> None:
        assert PollerOutcome.no_candidates.value == "no_candidates"
        assert PollerOutcome.handed_off.value == "handed_off"
        assert PollerOutcome.query_failed.value == "query_failed"

    def test_enum_has_exactly_three_members(self) -> None:
        assert len(PollerOutcome) == 3


# ---------------------------------------------------------------------------
# PollerResult
# ---------------------------------------------------------------------------


class TestPollerResult:
    def test_no_candidates_result(self) -> None:
        result = PollerResult(outcome=PollerOutcome.no_candidates)
        assert result.outcome is PollerOutcome.no_candidates
        assert result.selected_item_id is None
        assert result.error is None

    def test_handed_off_result(self) -> None:
        result = PollerResult(
            outcome=PollerOutcome.handed_off,
            selected_item_id="WL-123",
        )
        assert result.outcome is PollerOutcome.handed_off
        assert result.selected_item_id == "WL-123"
        assert result.error is None

    def test_query_failed_result(self) -> None:
        result = PollerResult(
            outcome=PollerOutcome.query_failed,
            error="non-zero exit code: 1",
        )
        assert result.outcome is PollerOutcome.query_failed
        assert result.selected_item_id is None
        assert result.error == "non-zero exit code: 1"

    def test_frozen(self) -> None:
        result = PollerResult(outcome=PollerOutcome.no_candidates)
        try:
            result.outcome = PollerOutcome.handed_off  # type: ignore[misc]
            assert False, "Expected FrozenInstanceError"
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# AuditHandoffHandler protocol
# ---------------------------------------------------------------------------


class _ValidHandler:
    """A handler that satisfies the AuditHandoffHandler protocol."""

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        return True


class _InvalidHandler:
    """A handler missing the required __call__ signature."""

    def do_audit(self, work_item: Dict[str, Any]) -> bool:
        return True


def _valid_function_handler(work_item: Dict[str, Any]) -> bool:
    """A bare function also satisfies the protocol."""
    return False


class TestAuditHandoffHandler:
    def test_class_satisfies_protocol(self) -> None:
        handler = _ValidHandler()
        assert isinstance(handler, AuditHandoffHandler)

    def test_function_satisfies_protocol(self) -> None:
        assert isinstance(_valid_function_handler, AuditHandoffHandler)

    def test_lambda_satisfies_protocol(self) -> None:
        handler = lambda work_item: True  # noqa: E731
        assert isinstance(handler, AuditHandoffHandler)

    def test_class_without_call_does_not_satisfy_protocol(self) -> None:
        handler = _InvalidHandler()
        assert not isinstance(handler, AuditHandoffHandler)

    def test_handler_can_be_called(self) -> None:
        handler: AuditHandoffHandler = _ValidHandler()
        item = {
            "id": "WL-1",
            "title": "Test",
            "status": "in-progress",
            "stage": "in_review",
        }
        assert handler(item) is True

    def test_handler_receives_work_item_dict(self) -> None:
        received: list[Dict[str, Any]] = []

        def capturing_handler(work_item: Dict[str, Any]) -> bool:
            received.append(work_item)
            return True

        item = {"id": "WL-42", "title": "Check", "stage": "in_review"}
        capturing_handler(item)
        assert len(received) == 1
        assert received[0] is item


# ---------------------------------------------------------------------------
# _query_candidates
# ---------------------------------------------------------------------------


def _make_proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Helper to build a subprocess.CompletedProcess for tests."""
    return subprocess.CompletedProcess(
        args="wl list --stage in_review --json",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestQueryCandidates:
    def test_empty_list_response(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="[]")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_list_response(self) -> None:
        items = [
            {"id": "WL-1", "title": "Item 1", "stage": "in_review"},
            {"id": "WL-2", "title": "Item 2", "stage": "in_review"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2
        assert result[0]["id"] == "WL-1"
        assert result[1]["id"] == "WL-2"

    def test_dict_workItems_response(self) -> None:
        payload = {
            "workItems": [
                {"id": "WL-1", "title": "Item 1"},
                {"id": "WL-2", "title": "Item 2"},
            ]
        }

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2

    def test_dict_items_response(self) -> None:
        payload = {"items": [{"id": "WL-10", "title": "A"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-10"

    def test_dict_data_response(self) -> None:
        payload = {"data": [{"id": "WL-20", "title": "B"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-20"

    def test_dict_work_items_response(self) -> None:
        payload = {"work_items": [{"id": "WL-30", "title": "C"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-30"

    def test_dict_fallback_workitems_key(self) -> None:
        payload = {"allWorkItems": [{"id": "WL-40", "title": "D"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-40"

    def test_deduplicates_by_id(self) -> None:
        items = [
            {"id": "WL-1", "title": "First"},
            {"id": "WL-1", "title": "Duplicate"},
            {"id": "WL-2", "title": "Second"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"WL-1", "WL-2"}

    def test_work_item_id_key(self) -> None:
        """Items using 'work_item_id' key are normalised to 'id'."""
        items = [{"work_item_id": "WL-50", "title": "E"}]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-50"

    def test_work_item_key(self) -> None:
        """Items using 'work_item' key are normalised to 'id'."""
        items = [{"work_item": "WL-60", "title": "F"}]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-60"

    def test_items_without_id_are_dropped(self) -> None:
        items = [
            {"id": "WL-1", "title": "Has ID"},
            {"title": "No ID"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-1"

    def test_non_zero_exit_code_returns_none(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(returncode=1, stderr="error")

        result = _query_candidates(run_shell, "/tmp")
        assert result is None

    def test_invalid_json_returns_none(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="not json at all")

        result = _query_candidates(run_shell, "/tmp")
        assert result is None

    def test_null_json_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="null")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_empty_string_stdout_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_run_shell_exception_returns_none(self) -> None:
        def run_shell(cmd, **kw):
            raise OSError("connection refused")

        result = _query_candidates(run_shell, "/tmp")
        assert result is None

    def test_passes_cwd_and_timeout(self) -> None:
        received_kwargs: list[dict] = []

        def run_shell(cmd, **kw):
            received_kwargs.append(kw)
            return _make_proc(stdout="[]")

        _query_candidates(run_shell, "/my/project", timeout=42)
        assert len(received_kwargs) == 1
        assert received_kwargs[0]["cwd"] == "/my/project"
        assert received_kwargs[0]["timeout"] == 42

    def test_passes_shell_true(self) -> None:
        """Verify shell=True is passed so the command string is interpreted
        by the shell rather than treated as a literal executable path."""
        received_kwargs: list[dict] = []

        def run_shell(cmd, **kw):
            received_kwargs.append(kw)
            return _make_proc(stdout="[]")

        _query_candidates(run_shell, "/tmp")
        assert len(received_kwargs) == 1
        assert received_kwargs[0].get("shell") is True, (
            "_query_candidates must pass shell=True to run_shell; "
            "without it subprocess.run treats the string as a filename"
        )


# ---------------------------------------------------------------------------
# _filter_by_cooldown
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 2, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


class TestFilterByCooldown:
    def test_all_eligible_no_store_entries(self) -> None:
        candidates = [
            {"id": "WL-1", "title": "A"},
            {"id": "WL-2", "title": "B"},
        ]
        result = _filter_by_cooldown(candidates, {}, cooldown_hours=6, now=_NOW)
        assert len(result) == 2

    def test_all_within_cooldown(self) -> None:
        # All items audited 1 hour ago, cooldown is 6 hours
        one_hour_ago = (_NOW - dt.timedelta(hours=1)).isoformat()
        store = {"WL-1": one_hour_ago, "WL-2": one_hour_ago}
        candidates = [{"id": "WL-1"}, {"id": "WL-2"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert result == []

    def test_none_within_cooldown(self) -> None:
        # All items audited 10 hours ago, cooldown is 6 hours
        ten_hours_ago = (_NOW - dt.timedelta(hours=10)).isoformat()
        store = {"WL-1": ten_hours_ago, "WL-2": ten_hours_ago}
        candidates = [{"id": "WL-1"}, {"id": "WL-2"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert len(result) == 2

    def test_mixed_cooldown(self) -> None:
        # WL-1 audited 1 hour ago (within cooldown), WL-2 10 hours ago (eligible)
        store = {
            "WL-1": (_NOW - dt.timedelta(hours=1)).isoformat(),
            "WL-2": (_NOW - dt.timedelta(hours=10)).isoformat(),
        }
        candidates = [{"id": "WL-1"}, {"id": "WL-2"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert len(result) == 1
        assert result[0]["id"] == "WL-2"

    def test_exact_boundary_is_eligible(self) -> None:
        # Exactly 6 hours ago with 6-hour cooldown -> eligible
        exactly_at = (_NOW - dt.timedelta(hours=6)).isoformat()
        store = {"WL-1": exactly_at}
        candidates = [{"id": "WL-1"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert len(result) == 1

    def test_just_under_boundary_is_filtered(self) -> None:
        # 5h59m ago with 6-hour cooldown -> still in cooldown
        just_under = (_NOW - dt.timedelta(hours=5, minutes=59)).isoformat()
        store = {"WL-1": just_under}
        candidates = [{"id": "WL-1"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert result == []

    def test_missing_store_entry_is_eligible(self) -> None:
        # WL-1 in store and within cooldown, WL-2 has no entry
        store = {"WL-1": (_NOW - dt.timedelta(hours=1)).isoformat()}
        candidates = [{"id": "WL-1"}, {"id": "WL-2"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert len(result) == 1
        assert result[0]["id"] == "WL-2"

    def test_invalid_iso_in_store_is_eligible(self) -> None:
        store = {"WL-1": "not-a-date"}
        candidates = [{"id": "WL-1"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert len(result) == 1

    def test_z_suffix_timestamps(self) -> None:
        # Z-suffix ISO format (common in JSON)
        two_hours_ago = (_NOW - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store = {"WL-1": two_hours_ago}
        candidates = [{"id": "WL-1"}]
        result = _filter_by_cooldown(candidates, store, cooldown_hours=6, now=_NOW)
        assert result == []  # 2 hours < 6 hours -> filtered

    def test_empty_candidates(self) -> None:
        result = _filter_by_cooldown([], {}, cooldown_hours=6, now=_NOW)
        assert result == []

    def test_items_without_id_are_dropped(self) -> None:
        candidates = [{"title": "No ID"}]
        result = _filter_by_cooldown(candidates, {}, cooldown_hours=6, now=_NOW)
        assert result == []


# ---------------------------------------------------------------------------
# _select_candidate
# ---------------------------------------------------------------------------


class TestSelectCandidate:
    def test_empty_list_returns_none(self) -> None:
        assert _select_candidate([]) is None

    def test_single_candidate(self) -> None:
        candidates = [{"id": "WL-1", "updatedAt": "2026-02-25T10:00:00Z"}]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] == "WL-1"

    def test_oldest_first(self) -> None:
        candidates = [
            {"id": "WL-new", "updatedAt": "2026-02-25T12:00:00Z"},
            {"id": "WL-old", "updatedAt": "2026-02-25T08:00:00Z"},
            {"id": "WL-mid", "updatedAt": "2026-02-25T10:00:00Z"},
        ]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] == "WL-old"

    def test_no_timestamp_sorted_first(self) -> None:
        candidates = [
            {"id": "WL-ts", "updatedAt": "2026-02-25T10:00:00Z"},
            {"id": "WL-no-ts"},
        ]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] == "WL-no-ts"

    def test_updated_at_key_variant(self) -> None:
        candidates = [
            {"id": "WL-1", "updated_at": "2026-02-25T12:00:00Z"},
            {"id": "WL-2", "updated_at": "2026-02-25T08:00:00Z"},
        ]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] == "WL-2"

    def test_identical_timestamps_selects_one(self) -> None:
        ts = "2026-02-25T10:00:00Z"
        candidates = [
            {"id": "WL-a", "updatedAt": ts},
            {"id": "WL-b", "updatedAt": ts},
        ]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] in ("WL-a", "WL-b")

    def test_all_missing_timestamps(self) -> None:
        candidates = [{"id": "WL-1"}, {"id": "WL-2"}]
        result = _select_candidate(candidates)
        assert result is not None
        assert result["id"] in ("WL-1", "WL-2")


# ---------------------------------------------------------------------------
# poll_and_handoff
# ---------------------------------------------------------------------------


class _MockStore:
    """Minimal mock for SchedulerStore."""

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        self._states: Dict[str, Dict[str, Any]] = {}
        if state is not None:
            self._states["test-cmd"] = state

    def get_state(self, command_id: str) -> Dict[str, Any]:
        return dict(self._states.get(command_id, {}))

    def update_state(self, command_id: str, state: Dict[str, Any]) -> None:
        self._states[command_id] = dict(state)


class _MockSpec:
    """Minimal mock for CommandSpec."""

    def __init__(
        self,
        command_id: str = "test-cmd",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.command_id = command_id
        self.metadata = metadata or {}


class TestPollAndHandoff:
    def test_full_flow_hands_off_candidate(self) -> None:
        items = [
            {"id": "WL-1", "title": "Item 1", "updatedAt": "2026-02-25T08:00:00Z"},
        ]
        handed: list[Dict[str, Any]] = []

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        def handler(work_item):
            handed.append(work_item)
            return True

        store = _MockStore()
        spec = _MockSpec(metadata={"audit_cooldown_hours": 6})

        result = poll_and_handoff(run_shell, "/tmp", store, spec, handler, now=_NOW)

        assert result.outcome is PollerOutcome.handed_off
        assert result.selected_item_id == "WL-1"
        assert len(handed) == 1
        assert handed[0]["id"] == "WL-1"

    def test_persists_timestamp_before_handoff(self) -> None:
        items = [{"id": "WL-5", "title": "X"}]
        persist_order: list[str] = []

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        class TrackingStore:
            def __init__(self):
                self.states: Dict[str, Any] = {}

            def get_state(self, cid):
                return dict(self.states.get(cid, {}))

            def update_state(self, cid, state):
                persist_order.append("store_updated")
                self.states[cid] = dict(state)

        def handler(work_item):
            persist_order.append("handler_called")
            return True

        store = TrackingStore()
        spec = _MockSpec()

        result = poll_and_handoff(run_shell, "/tmp", store, spec, handler, now=_NOW)

        assert result.outcome is PollerOutcome.handed_off
        assert persist_order == ["store_updated", "handler_called"]
        # Verify timestamp was actually written
        state = store.states.get("test-cmd", {})
        assert "WL-5" in state.get("last_audit_at_by_item", {})

    def test_no_candidates_returns_no_candidates(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="[]")

        result = poll_and_handoff(
            run_shell,
            "/tmp",
            _MockStore(),
            _MockSpec(),
            lambda work_item: True,
            now=_NOW,
        )
        assert result.outcome is PollerOutcome.no_candidates
        assert result.selected_item_id is None

    def test_all_within_cooldown_returns_no_candidates(self) -> None:
        items = [{"id": "WL-1", "title": "A"}]
        one_hour_ago = (_NOW - dt.timedelta(hours=1)).isoformat()
        store = _MockStore({"last_audit_at_by_item": {"WL-1": one_hour_ago}})

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = poll_and_handoff(
            run_shell,
            "/tmp",
            store,
            _MockSpec(metadata={"audit_cooldown_hours": 6}),
            lambda work_item: True,
            now=_NOW,
        )
        assert result.outcome is PollerOutcome.no_candidates

    def test_query_failure_returns_query_failed(self) -> None:
        """When wl list exits non-zero, _query_candidates returns None, which
        is reported as query_failed."""

        def run_shell(cmd, **kw):
            return _make_proc(returncode=1, stderr="fail")

        result = poll_and_handoff(
            run_shell,
            "/tmp",
            _MockStore(),
            _MockSpec(),
            lambda work_item: True,
            now=_NOW,
        )
        assert result.outcome is PollerOutcome.query_failed
        assert result.error is not None

    def test_handler_exception_still_returns_handed_off(self) -> None:
        items = [{"id": "WL-99", "title": "Explode"}]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        def handler(work_item):
            raise RuntimeError("handler boom")

        result = poll_and_handoff(
            run_shell, "/tmp", _MockStore(), _MockSpec(), handler, now=_NOW
        )
        assert result.outcome is PollerOutcome.handed_off
        assert result.selected_item_id == "WL-99"

    def test_selects_oldest_candidate(self) -> None:
        items = [
            {"id": "WL-new", "title": "New", "updatedAt": "2026-02-25T12:00:00Z"},
            {"id": "WL-old", "title": "Old", "updatedAt": "2026-02-25T06:00:00Z"},
        ]
        handed: list[Dict[str, Any]] = []

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = poll_and_handoff(
            run_shell,
            "/tmp",
            _MockStore(),
            _MockSpec(),
            lambda work_item: handed.append(work_item) or True,
            now=_NOW,
        )
        assert result.selected_item_id == "WL-old"

    def test_default_cooldown_when_metadata_missing(self) -> None:
        """When spec has no metadata, uses default cooldown of 6 hours."""
        items = [{"id": "WL-1", "title": "A"}]
        # Audited 3 hours ago - should be within default 6h cooldown
        three_hours_ago = (_NOW - dt.timedelta(hours=3)).isoformat()
        store = _MockStore({"last_audit_at_by_item": {"WL-1": three_hours_ago}})

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = poll_and_handoff(
            run_shell,
            "/tmp",
            store,
            _MockSpec(metadata={}),
            lambda work_item: True,
            now=_NOW,
        )
        assert result.outcome is PollerOutcome.no_candidates
