"""Integration tests for per-project isolation of .env and scheduler state.

Verifies that two projects sharing a single global AMPA install get fully
isolated .env values, scheduler_store.json state, and delegation dedup
(last_delegation_report_hash).

Covers acceptance criteria from SA-0MLUDW1XP00DCUJT.
"""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from pathlib import Path
from typing import Dict, Any
from unittest import mock

import pytest

from ampa.daemon import _project_ampa_dir, load_env
from ampa.scheduler_types import SchedulerConfig
from ampa.scheduler_store import SchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env(project_dir: Path, env_vars: Dict[str, str]) -> Path:
    """Write a .env file to ``<project_dir>/.worklog/ampa/.env``."""
    ampa_dir = project_dir / ".worklog" / "ampa"
    ampa_dir.mkdir(parents=True, exist_ok=True)
    env_path = ampa_dir / ".env"
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    env_path.write_text("\n".join(lines) + "\n")
    return env_path


def _write_store(project_dir: Path, store_data: Dict[str, Any]) -> Path:
    """Write a scheduler_store.json to ``<project_dir>/.worklog/ampa/``."""
    ampa_dir = project_dir / ".worklog" / "ampa"
    ampa_dir.mkdir(parents=True, exist_ok=True)
    store_path = ampa_dir / "scheduler_store.json"
    store_path.write_text(json.dumps(store_data, indent=2))
    return store_path


def _minimal_store(
    *,
    commands: Dict[str, Any] | None = None,
    state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a minimal valid scheduler store dict."""
    return {
        "commands": commands or {},
        "state": state or {},
        "last_global_start_ts": None,
        "dispatches": [],
    }


def _content_hash(text: str) -> str:
    """SHA-256 hash matching ampa.delegation._content_hash."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Save and restore os.environ around every test.

    load_env() calls load_dotenv(override=True) which mutates os.environ
    directly.  monkeypatch only restores values it explicitly patches, so
    without this fixture env vars leak across test boundaries and cause
    downstream tests (test_scheduler_run, test_scheduler_scoring,
    test_stale_delegation_watchdog) to hang.
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture()
def project_a(tmp_path: Path) -> Path:
    """Temporary project directory A with its own .env and store."""
    proj = tmp_path / "project_a"
    proj.mkdir()
    _write_env(
        proj,
        {
            "AMPA_DISCORD_BOT_TOKEN": "https://discord.example.com/bot-token/project-a",
            "AMPA_HEARTBEAT_MINUTES": "5",
        },
    )
    _write_store(
        proj,
        _minimal_store(
            commands={
                "cmd-a": {
                    "id": "cmd-a",
                    "command": "echo project-a",
                    "requires_llm": False,
                    "frequency_minutes": 1,
                    "priority": 0,
                    "metadata": {},
                    "type": "shell",
                },
            },
            state={
                "cmd-a": {
                    "last_delegation_report_hash": _content_hash("report-from-a"),
                },
            },
        ),
    )
    return proj


@pytest.fixture()
def project_b(tmp_path: Path) -> Path:
    """Temporary project directory B with its own .env and store."""
    proj = tmp_path / "project_b"
    proj.mkdir()
    _write_env(
        proj,
        {
            "AMPA_DISCORD_BOT_TOKEN": "https://discord.example.com/bot-token/project-b",
            "AMPA_HEARTBEAT_MINUTES": "10",
        },
    )
    _write_store(
        proj,
        _minimal_store(
            commands={
                "cmd-b": {
                    "id": "cmd-b",
                    "command": "echo project-b",
                    "requires_llm": False,
                    "frequency_minutes": 2,
                    "priority": 1,
                    "metadata": {},
                    "type": "shell",
                },
            },
            state={
                "cmd-b": {
                    "last_delegation_report_hash": _content_hash("report-from-b"),
                },
            },
        ),
    )
    return proj


@pytest.fixture()
def global_install_dir(tmp_path: Path) -> Path:
    """Simulated global AMPA install directory (empty .env, no store).

    This emulates ``~/.config/opencode/.worklog/plugins/ampa_py/``.
    The key property is that it does NOT contain per-project state;
    when the daemon resolves paths via ``os.getcwd()``, it must prefer
    the project-local directory over this global one.
    """
    gdir = tmp_path / "global_ampa_install"
    gdir.mkdir()
    # Place a global .env with a DIFFERENT bot token value -- tests verify that
    # project-local .env takes precedence over package-local .env.
    (gdir / ".env").write_text(
        "AMPA_DISCORD_BOT_TOKEN=https://discord.example.com/bot-token/global\n"
    )
    return gdir


# ---------------------------------------------------------------------------
# Test: _project_ampa_dir resolves per-project paths
# ---------------------------------------------------------------------------


class TestProjectAmpaDir:
    """Verify _project_ampa_dir() returns cwd-based paths."""

    def test_returns_cwd_based_path(self, project_a: Path, monkeypatch):
        monkeypatch.chdir(project_a)
        result = _project_ampa_dir()
        assert result == str(project_a / ".worklog" / "ampa")

    def test_different_projects_get_different_dirs(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        monkeypatch.chdir(project_a)
        dir_a = _project_ampa_dir()

        monkeypatch.chdir(project_b)
        dir_b = _project_ampa_dir()

        assert dir_a != dir_b
        assert "project_a" in dir_a
        assert "project_b" in dir_b


# ---------------------------------------------------------------------------
# Test: load_env reads per-project .env files
# ---------------------------------------------------------------------------


class TestLoadEnvIsolation:
    """Verify load_env() reads the correct per-project .env file."""

    def test_project_a_loads_its_own_env(
        self, project_a: Path, global_install_dir: Path, monkeypatch
    ):
        """Daemon started in project A reads project A's .env values."""
        # Clear any existing env vars that would interfere
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_HEARTBEAT_MINUTES", raising=False)
        monkeypatch.chdir(project_a)

        load_env()

        assert os.getenv("AMPA_DISCORD_BOT_TOKEN") == (
            "https://discord.example.com/bot-token/project-a"
        )
        assert os.getenv("AMPA_HEARTBEAT_MINUTES") == "5"

    def test_project_b_loads_its_own_env(
        self, project_b: Path, global_install_dir: Path, monkeypatch
    ):
        """Daemon started in project B reads project B's .env values."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_HEARTBEAT_MINUTES", raising=False)
        monkeypatch.chdir(project_b)

        load_env()

        assert os.getenv("AMPA_DISCORD_BOT_TOKEN") == (
            "https://discord.example.com/bot-token/project-b"
        )
        assert os.getenv("AMPA_HEARTBEAT_MINUTES") == "10"

    def test_sequential_loads_isolate_correctly(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """Loading .env from project A then project B gives B's values."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_HEARTBEAT_MINUTES", raising=False)

        # Load project A
        monkeypatch.chdir(project_a)
        load_env()
        token_a = os.getenv("AMPA_DISCORD_BOT_TOKEN")

        # Load project B (overrides)
        monkeypatch.chdir(project_b)
        load_env()
        token_b = os.getenv("AMPA_DISCORD_BOT_TOKEN")

        assert token_a == "https://discord.example.com/bot-token/project-a"
        assert token_b == "https://discord.example.com/bot-token/project-b"
        assert token_a != token_b

    def test_project_env_takes_precedence_over_global(
        self, project_a: Path, global_install_dir: Path, monkeypatch
    ):
        """Per-project .env takes precedence over the global install .env."""
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.chdir(project_a)

        load_env()

        # project_a's .env should win over global_install_dir's .env
        token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
        assert token == "https://discord.example.com/bot-token/project-a"
        assert "global" not in token


# ---------------------------------------------------------------------------
# Test: SchedulerConfig.from_env resolves per-project store path
# ---------------------------------------------------------------------------


class TestSchedulerConfigIsolation:
    """Verify SchedulerConfig.from_env() resolves per-project store_path."""

    def test_project_a_store_path(self, project_a: Path, monkeypatch):
        monkeypatch.chdir(project_a)
        config = SchedulerConfig.from_env()
        expected = str(project_a / ".worklog" / "ampa" / "scheduler_store.json")
        assert config.store_path == expected

    def test_project_b_store_path(self, project_b: Path, monkeypatch):
        monkeypatch.chdir(project_b)
        config = SchedulerConfig.from_env()
        expected = str(project_b / ".worklog" / "ampa" / "scheduler_store.json")
        assert config.store_path == expected

    def test_different_projects_get_different_store_paths(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()

        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()

        assert config_a.store_path != config_b.store_path
        assert "project_a" in config_a.store_path
        assert "project_b" in config_b.store_path


# ---------------------------------------------------------------------------
# Test: SchedulerStore reads per-project state files
# ---------------------------------------------------------------------------


class TestSchedulerStoreIsolation:
    """Verify SchedulerStore reads the correct per-project store file."""

    def test_project_a_store_has_its_own_commands(self, project_a: Path, monkeypatch):
        monkeypatch.chdir(project_a)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        commands = store.list_commands()
        command_ids = [c.command_id for c in commands]
        assert "cmd-a" in command_ids
        assert "cmd-b" not in command_ids

    def test_project_b_store_has_its_own_commands(self, project_b: Path, monkeypatch):
        monkeypatch.chdir(project_b)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        commands = store.list_commands()
        command_ids = [c.command_id for c in commands]
        assert "cmd-b" in command_ids
        assert "cmd-a" not in command_ids

    def test_stores_are_independent(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """Changes to store A do not affect store B."""
        # Load store A and add a command
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        from ampa.scheduler_types import CommandSpec

        store_a.add_command(
            CommandSpec(
                command_id="new-cmd",
                command="echo new",
                requires_llm=False,
                frequency_minutes=1,
                priority=0,
                metadata={},
            )
        )

        # Verify store B is unaffected
        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)

        b_ids = [c.command_id for c in store_b.list_commands()]
        assert "new-cmd" not in b_ids
        assert "cmd-b" in b_ids

    def test_store_writes_to_correct_project_path(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """SchedulerStore.save() persists to the project-local path only."""
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        store_a.data["state"]["cmd-a"]["marker"] = "written-by-a"
        store_a.save()

        # Reload store A and verify the marker is there
        store_a_reloaded = SchedulerStore(config_a.store_path)
        assert store_a_reloaded.data["state"]["cmd-a"]["marker"] == "written-by-a"

        # Verify store B does not have the marker
        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)
        assert "marker" not in store_b.data["state"].get("cmd-b", {})


# ---------------------------------------------------------------------------
# Test: Delegation dedup state (last_delegation_report_hash) isolation
# ---------------------------------------------------------------------------


class TestDelegationDedupIsolation:
    """Verify last_delegation_report_hash is isolated per project."""

    def test_project_a_has_its_own_dedup_hash(self, project_a: Path, monkeypatch):
        monkeypatch.chdir(project_a)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        state = store.get_state("cmd-a")
        assert state["last_delegation_report_hash"] == _content_hash("report-from-a")

    def test_project_b_has_its_own_dedup_hash(self, project_b: Path, monkeypatch):
        monkeypatch.chdir(project_b)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        state = store.get_state("cmd-b")
        assert state["last_delegation_report_hash"] == _content_hash("report-from-b")

    def test_dedup_hashes_differ_between_projects(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        hash_a = store_a.get_state("cmd-a").get("last_delegation_report_hash")

        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)
        hash_b = store_b.get_state("cmd-b").get("last_delegation_report_hash")

        assert hash_a is not None
        assert hash_b is not None
        assert hash_a != hash_b

    def test_updating_dedup_hash_in_a_does_not_affect_b(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """Updating last_delegation_report_hash in project A's store
        does not change project B's store."""
        # Update hash in project A
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        state_a = store_a.get_state("cmd-a")
        new_hash = _content_hash("updated-report-from-a")
        state_a["last_delegation_report_hash"] = new_hash
        store_a.update_state("cmd-a", state_a)

        # Verify project B is untouched
        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)
        state_b = store_b.get_state("cmd-b")
        assert state_b["last_delegation_report_hash"] == _content_hash("report-from-b")

        # Re-verify project A has the updated hash
        monkeypatch.chdir(project_a)
        store_a2 = SchedulerStore(config_a.store_path)
        assert store_a2.get_state("cmd-a")["last_delegation_report_hash"] == new_hash

    def test_dispatch_records_isolated(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """Dispatch records appended to store A are not in store B."""
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        store_a.append_dispatch(
            {
                "action": "delegated",
                "command_id": "cmd-a",
                "target": "agent-1",
            }
        )

        # Store B should have zero dispatches
        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)
        assert len(store_b.data.get("dispatches", [])) == 0

        # Store A should have one dispatch
        store_a_reloaded = SchedulerStore(config_a.store_path)
        assert len(store_a_reloaded.data.get("dispatches", [])) == 1


# ---------------------------------------------------------------------------
# Test: Cross-project contamination guard
# ---------------------------------------------------------------------------


class TestCrossProjectContamination:
    """Verify that one project cannot read or modify another project's state."""

    def test_project_a_cannot_see_project_b_store(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """When cwd is project A, SchedulerConfig points to A's store only."""
        monkeypatch.chdir(project_a)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        # Should only see project A's commands
        command_ids = [c.command_id for c in store.list_commands()]
        assert command_ids == ["cmd-a"]

    def test_project_b_cannot_see_project_a_store(
        self, project_a: Path, project_b: Path, monkeypatch
    ):
        """When cwd is project B, SchedulerConfig points to B's store only."""
        monkeypatch.chdir(project_b)
        config = SchedulerConfig.from_env()
        store = SchedulerStore(config.store_path)

        command_ids = [c.command_id for c in store.list_commands()]
        assert command_ids == ["cmd-b"]

    def test_simultaneous_stores_are_independent(
        self, project_a: Path, project_b: Path
    ):
        """Two SchedulerStore instances opened concurrently target separate files."""
        path_a = str(project_a / ".worklog" / "ampa" / "scheduler_store.json")
        path_b = str(project_b / ".worklog" / "ampa" / "scheduler_store.json")

        store_a = SchedulerStore(path_a)
        store_b = SchedulerStore(path_b)

        # Verify they loaded different data
        a_ids = {c.command_id for c in store_a.list_commands()}
        b_ids = {c.command_id for c in store_b.list_commands()}

        assert a_ids == {"cmd-a"}
        assert b_ids == {"cmd-b"}
        assert a_ids.isdisjoint(b_ids)

    def test_modifying_store_a_leaves_store_b_intact(
        self, project_a: Path, project_b: Path
    ):
        """Mutating store A on disk does not affect store B on disk."""
        path_a = str(project_a / ".worklog" / "ampa" / "scheduler_store.json")
        path_b = str(project_b / ".worklog" / "ampa" / "scheduler_store.json")

        store_a = SchedulerStore(path_a)
        store_b_before = json.loads(Path(path_b).read_text())

        # Mutate store A
        from ampa.scheduler_types import CommandSpec

        store_a.add_command(
            CommandSpec(
                command_id="injected",
                command="echo injected",
                requires_llm=False,
                frequency_minutes=99,
                priority=99,
                metadata={"source": "project-a"},
            )
        )
        store_a.data["state"]["injected"] = {
            "last_delegation_report_hash": "deadbeef",
        }
        store_a.save()

        # Store B file should be byte-identical to what it was before
        store_b_after = json.loads(Path(path_b).read_text())
        assert store_b_before == store_b_after


# ---------------------------------------------------------------------------
# Test: Full lifecycle simulation (daemon init path)
# ---------------------------------------------------------------------------


class TestDaemonInitIsolation:
    """Simulate two daemon initializations and verify isolation end-to-end."""

    def test_two_daemons_get_isolated_configs(
        self,
        project_a: Path,
        project_b: Path,
        global_install_dir: Path,
        monkeypatch,
    ):
        """Two daemons initialized in different project dirs get fully
        isolated configurations and store paths."""
        # Simulate daemon A initialization
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_HEARTBEAT_MINUTES", raising=False)
        monkeypatch.chdir(project_a)
        load_env()
        token_a = os.getenv("AMPA_DISCORD_BOT_TOKEN")
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)

        # Capture state from daemon A
        a_token = token_a
        a_store_path = config_a.store_path
        a_commands = {c.command_id for c in store_a.list_commands()}
        a_dedup_hash = store_a.get_state("cmd-a").get("last_delegation_report_hash")

        # Simulate daemon B initialization
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_HEARTBEAT_MINUTES", raising=False)
        monkeypatch.chdir(project_b)
        load_env()
        token_b = os.getenv("AMPA_DISCORD_BOT_TOKEN")
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)

        b_token = token_b
        b_store_path = config_b.store_path
        b_commands = {c.command_id for c in store_b.list_commands()}
        b_dedup_hash = store_b.get_state("cmd-b").get("last_delegation_report_hash")

        # Assertions: everything is isolated
        assert a_token != b_token, "Bot tokens must differ"
        assert a_store_path != b_store_path, "Store paths must differ"
        assert a_commands != b_commands, "Commands must differ"
        assert a_commands == {"cmd-a"}
        assert b_commands == {"cmd-b"}
        assert a_dedup_hash != b_dedup_hash, "Dedup hashes must differ"
        assert a_dedup_hash == _content_hash("report-from-a")
        assert b_dedup_hash == _content_hash("report-from-b")

    def test_daemon_writes_isolated_to_its_own_store(
        self,
        project_a: Path,
        project_b: Path,
        monkeypatch,
    ):
        """A daemon writing to its store does not contaminate the other project."""
        # Daemon A writes state
        monkeypatch.chdir(project_a)
        config_a = SchedulerConfig.from_env()
        store_a = SchedulerStore(config_a.store_path)
        store_a.update_state(
            "cmd-a",
            {
                "last_delegation_report_hash": _content_hash("new-a-report"),
                "last_run_ts": "2026-02-22T00:00:00+00:00",
            },
        )

        # Daemon B's store is unmodified
        monkeypatch.chdir(project_b)
        config_b = SchedulerConfig.from_env()
        store_b = SchedulerStore(config_b.store_path)

        state_b = store_b.get_state("cmd-b")
        assert state_b["last_delegation_report_hash"] == _content_hash("report-from-b")
        assert "last_run_ts" not in state_b
