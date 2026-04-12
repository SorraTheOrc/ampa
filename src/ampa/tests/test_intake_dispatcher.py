from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from ampa.engine.dispatch import (
    DispatchResult,
    DryRunDispatcher,
    IntakeDispatcher,
    OpenCodeRunDispatcher,
)


def test_intake_dispatch_builds_command_and_delegates():
    dry = DryRunDispatcher()
    d = IntakeDispatcher(runner=d)

    res = d.dispatch(command="ignored", work_item_id="WL-INTAKE-1")

    assert res.success is True
    assert 'opencode run --agent Casey --command intake WL-INTAKE-1 do not ask questions' in res.command


def test_intake_dispatch_respects_timeout_and_starts_timer():
    mock_runner = MagicMock()
    mock_result = DispatchResult(
        success=True,
        command='opencode run --agent Casey --command intake WL-TIMEOUT do not ask questions',
        work_item_id="WL-TIMEOUT",
        timestamp=None,
        pid=5555,
    )
    mock_runner.dispatch.return_value = mock_result

    with patch("ampa.engine.dispatch.threading.Timer") as mock_timer_cls:
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        d = IntakeDispatcher(runner=mock_runner, timeout=7)
        res = d.dispatch(command="ignored", work_item_id="WL-TIMEOUT")

        assert res.success is True
        mock_timer_cls.assert_called_once()
        mock_timer.start.assert_called_once()


def test_integration_intake_runner_dispatch(monkeypatch):
    # Ensure IntakeRunner attempts to dispatch and writes a wl comment via run_shell.
    from ampa.intake_runner import IntakeRunner

    runner = IntakeRunner(run_shell=lambda *a, **k: None, command_cwd="/tmp")

    # Patch IntakeCandidateSelector to return a deterministic selection
    class FakeSelector:
        def __init__(self, **kwargs):
            pass

        def query_candidates(self):
            return [{"id": "WL-TEST-1", "title": "Test item"}]

        def select_top(self, candidates):
            return candidates[0]

    monkeypatch.setattr("ampa.intake_runner.IntakeCandidateSelector", FakeSelector)

    # Patch IntakeDispatcher to avoid spawning processes and return success
    class FakeDispatcher:
        def dispatch(self, command, work_item_id):
            from ampa.engine.dispatch import DispatchResult

            return DispatchResult(
                success=True,
                command=command,
                work_item_id=work_item_id,
                timestamp=None,
                pid=4242,
            )

    monkeypatch.setattr("ampa.intake_runner.IntakeDispatcher", lambda: FakeDispatcher())

    # Use a dummy store with minimal interface
    class DummyStore:
        def __init__(self):
            self._state = {}

        def get_state(self, cid):
            return self._state.get(cid, {})

        def update_state(self, cid, state):
            self._state[cid] = state

    store = DummyStore()
    res = runner.run(type("S", (), {"command_id": "intake-selector"})(), store)
    assert res["selected"] == "WL-TEST-1"
    assert res["dispatch"] is True


def test_intake_uses_env_var_timeout(monkeypatch):
    monkeypatch.setenv("AMPA_INTAKE_TIMEOUT", "13")
    d = IntakeDispatcher(runner=DryRunDispatcher())
    assert d._timeout == 13
