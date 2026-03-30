"""Comprehensive tests for ampa.fallback — modes, config, overrides, migration.

Covers:
- accept-recommendation: auto-accept when recommendation present, hold when absent
- discuss-options: fallback to hold with logging
- Per-decision override resolution
- AMPA_FALLBACK_MODE env override precedence
- Config file migration / new location
- Backward compatibility with legacy config format
- normalize_mode aliases for new modes
- save_config / load_config with overrides
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from ampa import fallback


# ---------------------------------------------------------------------------
# normalize_mode tests
# ---------------------------------------------------------------------------


class TestNormalizeModeNewModes:
    """Verify normalize_mode handles new mode values and aliases."""

    def test_accept_recommendation_canonical(self):
        assert (
            fallback.normalize_mode("accept-recommendation") == "accept-recommendation"
        )

    def test_accept_recommendation_underscore(self):
        assert (
            fallback.normalize_mode("accept_recommendation") == "accept-recommendation"
        )

    def test_accept_recommendation_alias_recommend(self):
        assert fallback.normalize_mode("recommend") == "accept-recommendation"

    def test_discuss_options_canonical(self):
        assert fallback.normalize_mode("discuss-options") == "discuss-options"

    def test_discuss_options_underscore(self):
        assert fallback.normalize_mode("discuss_options") == "discuss-options"

    def test_discuss_options_alias_discuss(self):
        assert fallback.normalize_mode("discuss") == "discuss-options"

    def test_case_insensitive(self):
        assert (
            fallback.normalize_mode("Accept-Recommendation") == "accept-recommendation"
        )
        assert fallback.normalize_mode("DISCUSS-OPTIONS") == "discuss-options"

    def test_unknown_falls_back_to_hold(self):
        assert fallback.normalize_mode("unknown-mode") == "hold"

    def test_none_falls_back_to_hold(self):
        assert fallback.normalize_mode(None) == "hold"

    def test_empty_falls_back_to_hold(self):
        assert fallback.normalize_mode("") == "hold"

    # Existing modes should still work
    def test_legacy_hold(self):
        assert fallback.normalize_mode("hold") == "hold"

    def test_legacy_auto_accept(self):
        assert fallback.normalize_mode("auto-accept") == "auto-accept"

    def test_legacy_auto_decline(self):
        assert fallback.normalize_mode("auto-decline") == "auto-decline"

    def test_legacy_alias_accept(self):
        assert fallback.normalize_mode("accept") == "auto-accept"

    def test_legacy_alias_decline(self):
        assert fallback.normalize_mode("decline") == "auto-decline"

    def test_legacy_alias_pause(self):
        assert fallback.normalize_mode("pause") == "hold"


# ---------------------------------------------------------------------------
# config_path tests — migration / location precedence
# ---------------------------------------------------------------------------


class TestConfigPath:
    """Verify config_path resolution order: env > new default > legacy > new default."""

    def test_env_override_takes_precedence(self, monkeypatch, tmp_path):
        override = str(tmp_path / "custom.json")
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", override)
        assert fallback.config_path() == override

    def test_new_default_when_file_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AMPA_FALLBACK_CONFIG_FILE", raising=False)
        # Create the new-location file
        new_loc = tmp_path / ".worklog" / "ampa"
        new_loc.mkdir(parents=True)
        cfg_file = new_loc / "fallback_config.json"
        cfg_file.write_text("{}")
        # Change cwd so the relative path resolves
        monkeypatch.chdir(tmp_path)
        result = fallback.config_path()
        assert result == os.path.join(".worklog", "ampa", "fallback_config.json")

    def test_legacy_fallback_when_only_old_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AMPA_FALLBACK_CONFIG_FILE", raising=False)
        # No new-location file
        monkeypatch.chdir(tmp_path)
        # Create legacy file
        legacy_dir = tmp_path / "tool_output"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "ampa_fallback_config.json"
        legacy_file.write_text("{}")
        result = fallback.config_path(tool_output_dir=str(legacy_dir))
        assert result == str(legacy_file)

    def test_returns_new_default_when_neither_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AMPA_FALLBACK_CONFIG_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = fallback.config_path()
        assert result == os.path.join(".worklog", "ampa", "fallback_config.json")


# ---------------------------------------------------------------------------
# load_config / save_config — per-decision overrides
# ---------------------------------------------------------------------------


class TestLoadConfigWithOverrides:
    """Verify load_config handles both legacy string entries and dict entries with overrides."""

    def test_legacy_string_projects(self, tmp_path):
        cfg = {
            "default": "auto-accept",
            "public_default": "hold",
            "projects": {"proj-a": "auto-accept", "proj-b": "hold"},
        }
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps(cfg))
        loaded = fallback.load_config(str(path))
        assert loaded["projects"]["proj-a"] == "auto-accept"
        assert loaded["projects"]["proj-b"] == "hold"

    def test_dict_projects_with_overrides(self, tmp_path):
        cfg = {
            "default": "hold",
            "public_default": "hold",
            "projects": {
                "proj-x": {
                    "mode": "accept-recommendation",
                    "overrides": {
                        "run-tests": "auto-accept",
                        "open-pr": "hold",
                    },
                }
            },
        }
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps(cfg))
        loaded = fallback.load_config(str(path))
        proj = loaded["projects"]["proj-x"]
        assert isinstance(proj, dict)
        assert proj["mode"] == "accept-recommendation"
        assert proj["overrides"]["run-tests"] == "auto-accept"
        assert proj["overrides"]["open-pr"] == "hold"

    def test_dict_projects_normalizes_modes(self, tmp_path):
        cfg = {
            "default": "hold",
            "projects": {
                "proj-y": {
                    "mode": "recommend",
                    "overrides": {"deploy": "discuss"},
                }
            },
        }
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps(cfg))
        loaded = fallback.load_config(str(path))
        proj = loaded["projects"]["proj-y"]
        assert proj["mode"] == "accept-recommendation"
        assert proj["overrides"]["deploy"] == "discuss-options"

    def test_missing_file_returns_defaults(self):
        loaded = fallback.load_config("/nonexistent/path.json")
        assert loaded["default"] == "hold"
        assert loaded["public_default"] == "hold"
        assert loaded["projects"] == {}


class TestSaveConfigWithOverrides:
    """Verify save_config persists dict entries with overrides correctly."""

    def test_round_trip_with_overrides(self, tmp_path):
        path = str(tmp_path / "cfg.json")
        cfg = {
            "default": "auto-accept",
            "public_default": "hold",
            "projects": {
                "proj-z": {
                    "mode": "accept-recommendation",
                    "overrides": {"run-tests": "auto-accept", "open-pr": "hold"},
                }
            },
        }
        saved = fallback.save_config(cfg, path)
        assert saved["projects"]["proj-z"]["mode"] == "accept-recommendation"
        assert saved["projects"]["proj-z"]["overrides"]["run-tests"] == "auto-accept"

        # Verify round-trip through load
        loaded = fallback.load_config(path)
        assert loaded["projects"]["proj-z"]["mode"] == "accept-recommendation"
        assert loaded["projects"]["proj-z"]["overrides"]["run-tests"] == "auto-accept"

    def test_dict_without_overrides_saves_as_string(self, tmp_path):
        path = str(tmp_path / "cfg.json")
        cfg = {
            "default": "hold",
            "projects": {"proj-simple": {"mode": "auto-accept", "overrides": {}}},
        }
        saved = fallback.save_config(cfg, path)
        # No overrides -> saved as simple string mode
        assert saved["projects"]["proj-simple"] == "auto-accept"

    def test_legacy_string_entries_preserved(self, tmp_path):
        path = str(tmp_path / "cfg.json")
        cfg = {"default": "hold", "projects": {"proj-old": "auto-decline"}}
        saved = fallback.save_config(cfg, path)
        assert saved["projects"]["proj-old"] == "auto-decline"


# ---------------------------------------------------------------------------
# resolve_mode — per-decision overrides
# ---------------------------------------------------------------------------


class TestResolveModeDecisionOverrides:
    """Verify resolve_mode honours per-decision overrides."""

    def test_decision_override_returned(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "hold",
            "projects": {
                "proj-d": {
                    "mode": "accept-recommendation",
                    "overrides": {"run-tests": "auto-accept", "open-pr": "hold"},
                }
            },
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode("proj-d", decision="run-tests") == "auto-accept"
        assert fallback.resolve_mode("proj-d", decision="open-pr") == "hold"

    def test_decision_not_in_overrides_falls_to_project_mode(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "hold",
            "projects": {
                "proj-d": {
                    "mode": "accept-recommendation",
                    "overrides": {"run-tests": "auto-accept"},
                }
            },
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert (
            fallback.resolve_mode("proj-d", decision="deploy")
            == "accept-recommendation"
        )

    def test_decision_with_legacy_string_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "hold",
            "projects": {"proj-legacy": "auto-accept"},
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        # Legacy entry has no overrides, so decision param is ignored
        assert (
            fallback.resolve_mode("proj-legacy", decision="run-tests") == "auto-accept"
        )

    def test_no_decision_returns_project_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "hold",
            "projects": {
                "proj-d": {
                    "mode": "accept-recommendation",
                    "overrides": {"run-tests": "auto-accept"},
                }
            },
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode("proj-d") == "accept-recommendation"


# ---------------------------------------------------------------------------
# resolve_mode — AMPA_FALLBACK_MODE env override precedence
# ---------------------------------------------------------------------------


class TestResolveModeEnvOverride:
    """Verify AMPA_FALLBACK_MODE env takes highest precedence."""

    def test_env_overrides_project_config(self, tmp_path, monkeypatch):
        cfg = {
            "default": "auto-accept",
            "projects": {"proj-e": "auto-accept"},
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        monkeypatch.setenv("AMPA_FALLBACK_MODE", "hold")
        assert fallback.resolve_mode("proj-e") == "hold"

    def test_env_overrides_decision_override(self, tmp_path, monkeypatch):
        cfg = {
            "default": "hold",
            "projects": {
                "proj-e": {
                    "mode": "hold",
                    "overrides": {"run-tests": "auto-accept"},
                }
            },
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        monkeypatch.setenv("AMPA_FALLBACK_MODE", "auto-decline")
        assert fallback.resolve_mode("proj-e", decision="run-tests") == "auto-decline"

    def test_env_accepts_new_modes(self, monkeypatch):
        monkeypatch.setenv("AMPA_FALLBACK_MODE", "accept-recommendation")
        assert fallback.resolve_mode(None) == "accept-recommendation"

    def test_env_accepts_discuss_options(self, monkeypatch):
        monkeypatch.setenv("AMPA_FALLBACK_MODE", "discuss-options")
        assert fallback.resolve_mode(None) == "discuss-options"


# ---------------------------------------------------------------------------
# resolve_mode — backward compatibility
# ---------------------------------------------------------------------------


class TestResolveModeBackwardCompat:
    """Existing modes and legacy config entries continue to work."""

    def test_hold_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {"default": "hold", "projects": {}}
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode(None) == "hold"

    def test_auto_accept_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "auto-accept",
            "public_default": "auto-accept",
            "projects": {},
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode(None) == "auto-accept"

    def test_auto_decline_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "auto-decline",
            "public_default": "auto-decline",
            "projects": {},
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode(None) == "auto-decline"

    def test_legacy_string_project_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        cfg = {
            "default": "hold",
            "projects": {"proj-old": "auto-accept"},
        }
        path = str(tmp_path / "cfg.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)
        assert fallback.resolve_mode("proj-old") == "auto-accept"

    def test_require_config_missing_returns_auto_accept(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)
        monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", str(tmp_path / "nope.json"))
        assert fallback.resolve_mode(None, require_config=True) == "auto-accept"


# ---------------------------------------------------------------------------
# Engine fallback mode tests — accept-recommendation and discuss-options
# ---------------------------------------------------------------------------


class TestEngineFallbackNewModes:
    """Test engine/core.py handles new fallback modes correctly."""

    def test_discuss_options_skips_delegation(self):
        from ampa.engine.core import Engine, EngineConfig, EngineStatus

        config = EngineConfig(fallback_mode="discuss-options")
        engine = _build_minimal_engine(config=config)
        result = engine.process_delegation()
        assert result.status == EngineStatus.SKIPPED
        assert "discuss-options" in result.reason

    def test_accept_recommendation_proceeds_to_dispatch(self):
        """accept-recommendation at the engine level should proceed like auto-accept."""
        from ampa.engine.core import Engine, EngineConfig, EngineStatus

        config = EngineConfig(fallback_mode="accept-recommendation")
        engine = _build_minimal_engine(config=config)
        result = engine.process_delegation()
        # Should NOT skip — should proceed through to dispatch
        assert result.status == EngineStatus.SUCCESS


# ---------------------------------------------------------------------------
# WSGI /respond endpoint — accept-recommendation and discuss-options
# ---------------------------------------------------------------------------


class TestRespondAcceptRecommendation:
    """Test the server.py /respond endpoint with accept-recommendation mode."""

    def test_auto_accepts_recommendation_when_present(
        self, tmp_path, monkeypatch, _metrics_fixture
    ):
        """When mode=accept-recommendation and payload has recommendation, auto-accept."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {"default": "hold", "projects": {"proj-rec": "accept-recommendation"}},
        )

        session_id = "s-rec-accept"
        conversation_manager.start_conversation(session_id, "Approve?")

        base, server = _metrics_fixture()
        payload = {
            "session_id": session_id,
            "project_id": "proj-rec",
            "recommendation": {"action": "accept", "reason": "All tests pass"},
        }
        resp = _post_json(f"{base}/respond", payload)
        assert resp["status"] == "resumed"
        assert resp["response"] == "accept"

    def test_falls_back_to_hold_when_no_recommendation(
        self, tmp_path, monkeypatch, _metrics_fixture
    ):
        """When mode=accept-recommendation but no recommendation field, fall back to hold."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {"default": "hold", "projects": {"proj-rec": "accept-recommendation"}},
        )

        session_id = "s-rec-hold"
        conversation_manager.start_conversation(session_id, "Approve?")

        base, server = _metrics_fixture()
        payload = {"session_id": session_id, "project_id": "proj-rec"}
        # No recommendation -> mode falls back to hold -> no action injected
        # -> responder raises ValueError (missing response)
        import urllib.error

        try:
            _post_json(f"{base}/respond", payload)
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode())
            assert "missing" in body["error"].lower()

    def test_recommendation_decline_action(
        self, tmp_path, monkeypatch, _metrics_fixture
    ):
        """When recommendation.action is 'decline', auto-decline."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {"default": "hold", "projects": {"proj-rec": "accept-recommendation"}},
        )

        session_id = "s-rec-decline"
        conversation_manager.start_conversation(session_id, "Approve?")

        base, server = _metrics_fixture()
        payload = {
            "session_id": session_id,
            "project_id": "proj-rec",
            "recommendation": {"action": "decline", "reason": "Tests failed"},
        }
        resp = _post_json(f"{base}/respond", payload)
        assert resp["status"] == "resumed"
        assert resp["response"] == "decline"


class TestRespondDiscussOptions:
    """Test the server.py /respond endpoint with discuss-options mode."""

    def test_discuss_options_falls_back_to_hold(
        self, tmp_path, monkeypatch, _metrics_fixture
    ):
        """discuss-options mode should not inject an action (fallback to hold)."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {"default": "hold", "projects": {"proj-disc": "discuss-options"}},
        )

        session_id = "s-disc"
        conversation_manager.start_conversation(session_id, "What should we do?")

        base, server = _metrics_fixture()
        payload = {"session_id": session_id, "project_id": "proj-disc"}
        import urllib.error

        try:
            _post_json(f"{base}/respond", payload)
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode())
            assert "missing" in body["error"].lower()


class TestRespondPerDecisionOverride:
    """Test per-decision overrides end-to-end through /respond."""

    def test_decision_override_applied(self, tmp_path, monkeypatch, _metrics_fixture):
        """When decision field is present, per-decision override applies."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {
                "default": "hold",
                "projects": {
                    "proj-over": {
                        "mode": "hold",
                        "overrides": {"run-tests": "auto-accept"},
                    }
                },
            },
        )

        session_id = "s-override"
        conversation_manager.start_conversation(session_id, "Run tests?")

        base, server = _metrics_fixture()
        payload = {
            "session_id": session_id,
            "project_id": "proj-over",
            "decision": "run-tests",
        }
        resp = _post_json(f"{base}/respond", payload)
        assert resp["status"] == "resumed"
        assert resp["response"] == "accept"

    def test_decision_not_overridden_uses_project_default(
        self, tmp_path, monkeypatch, _metrics_fixture
    ):
        """When decision has no override, project default applies."""
        from ampa import conversation_manager

        monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
        _setup_fallback_config(
            tmp_path,
            monkeypatch,
            {
                "default": "hold",
                "projects": {
                    "proj-over": {
                        "mode": "hold",
                        "overrides": {"run-tests": "auto-accept"},
                    }
                },
            },
        )

        session_id = "s-no-override"
        conversation_manager.start_conversation(session_id, "Open PR?")

        base, server = _metrics_fixture()
        payload = {
            "session_id": session_id,
            "project_id": "proj-over",
            "decision": "open-pr",
        }
        # hold mode -> no action injected -> missing response -> 400
        import urllib.error

        try:
            _post_json(f"{base}/respond", payload)
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400


# ---------------------------------------------------------------------------
# VALID_MODES set includes new modes
# ---------------------------------------------------------------------------


class TestValidModesSet:
    def test_valid_modes_includes_new(self):
        assert "accept-recommendation" in fallback.VALID_MODES
        assert "discuss-options" in fallback.VALID_MODES

    def test_valid_modes_includes_legacy(self):
        assert "hold" in fallback.VALID_MODES
        assert "auto-accept" in fallback.VALID_MODES
        assert "auto-decline" in fallback.VALID_MODES


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _setup_fallback_config(tmp_path, monkeypatch, config):
    """Write a fallback config and point the env at it."""
    path = str(tmp_path / "fallback_config.json")
    with open(path, "w") as f:
        json.dump(config, f)
    monkeypatch.setenv("AMPA_FALLBACK_CONFIG_FILE", path)


def _post_json(url, payload):
    """POST JSON to a URL and return parsed response body."""
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read().decode())


@pytest.fixture()
def _metrics_fixture(monkeypatch):
    """Start the AMPA server; yield a factory that returns (base_url, server)."""
    from ampa.server import start_metrics_server

    servers = []

    def _start(**kwargs):
        server, port = start_metrics_server(port=0, **kwargs)
        servers.append(server)
        return f"http://127.0.0.1:{port}", server

    yield _start

    for srv in servers:
        if srv._server:
            srv._server[0].shutdown()


def _build_minimal_engine(config=None):
    """Build a minimal Engine for testing fallback mode behaviour."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from ampa.engine.core import Engine, EngineConfig
    from ampa.engine.candidates import CandidateResult, WorkItemCandidate
    from ampa.engine.descriptor import Command, StateTuple, WorkflowDescriptor
    from ampa.engine.dispatch import DispatchResult

    FIXED_TIME = datetime(2026, 2, 24, 0, 0, 0, tzinfo=timezone.utc)

    # Minimal descriptor with a 'delegate' command
    ready_state = StateTuple(status="open", stage="plan_complete")
    in_progress_state = StateTuple(status="in-progress", stage="in_progress")

    delegate_cmd = Command(
        name="delegate",
        description="Delegate work item to agent",
        from_states=["ready"],
        to="in_progress",
        actor="scheduler",
        dispatch_map={"ready": "implement {id}"},
    )

    descriptor = MagicMock(spec=WorkflowDescriptor)
    descriptor.get_command.return_value = delegate_cmd
    descriptor.commands = {"delegate": delegate_cmd}
    descriptor.resolve_state_ref.side_effect = lambda ref: {
        "ready": ready_state,
        "in_progress": in_progress_state,
    }.get(ref, in_progress_state)
    descriptor.resolve_from_state_alias.return_value = "ready"

    # Candidate selector returns a valid candidate
    candidate = WorkItemCandidate(
        id="WL-TEST",
        title="Test item",
        status="open",
        stage="plan_complete",
        priority="medium",
    )
    selector = MagicMock()
    selector.select.return_value = CandidateResult(
        selected=candidate,
        candidates=(candidate,),
    )

    # Dispatcher
    dispatcher = MagicMock()
    dispatch_result = DispatchResult(
        success=True,
        pid=12345,
        command="implement WL-TEST",
        work_item_id="WL-TEST",
        timestamp=FIXED_TIME,
    )
    dispatcher.dispatch.return_value = dispatch_result

    # Invariant evaluator (no invariants)
    evaluator = MagicMock()

    # Fetcher
    fetcher = MagicMock()
    fetcher.fetch.return_value = {
        "workItem": {
            "id": "WL-TEST",
            "status": "open",
            "stage": "plan_complete",
        }
    }

    config = config or EngineConfig()

    return Engine(
        descriptor=descriptor,
        dispatcher=dispatcher,
        candidate_selector=selector,
        invariant_evaluator=evaluator,
        work_item_fetcher=fetcher,
        config=config,
        clock=lambda: FIXED_TIME,
    )
