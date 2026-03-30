"""Tests for ampa.run_gh_sync wrapper module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ampa.run_gh_sync import (
    _repo_from_config,
    _detect_repo_from_remote,
    ensure_repo_configured,
    run_sync,
    main,
    _GH_SSH_RE,
    _GH_HTTPS_RE,
)


# ---------------------------------------------------------------------------
# _repo_from_config
# ---------------------------------------------------------------------------


class TestRepoFromConfig:
    def test_returns_repo_when_set(self):
        assert _repo_from_config({"githubRepo": "owner/repo"}) == "owner/repo"

    def test_returns_none_when_missing(self):
        assert _repo_from_config({}) is None

    def test_returns_none_when_not_set(self):
        assert _repo_from_config({"githubRepo": "(not set)"}) is None

    def test_returns_none_when_empty_string(self):
        assert _repo_from_config({"githubRepo": ""}) is None

    def test_returns_none_when_whitespace(self):
        assert _repo_from_config({"githubRepo": "  "}) is None

    def test_strips_whitespace(self):
        assert _repo_from_config({"githubRepo": "  owner/repo  "}) == "owner/repo"


# ---------------------------------------------------------------------------
# _detect_repo_from_remote
# ---------------------------------------------------------------------------


class TestDetectRepoFromRemote:
    def test_ssh_url(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="git@github.com:owner/repo.git\n", stderr=""
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() == "owner/repo"

    def test_ssh_url_without_dot_git(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="git@github.com:owner/repo\n", stderr=""
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() == "owner/repo"

    def test_https_url(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
            stderr="",
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() == "owner/repo"

    def test_https_url_without_dot_git(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/owner/repo\n", stderr=""
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() == "owner/repo"

    def test_non_github_url_returns_none(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
            stderr="",
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() is None

    def test_git_command_fails_returns_none(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: not a git repository"
        )
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert _detect_repo_from_remote() is None

    def test_git_command_exception_returns_none(self):
        with patch(
            "ampa.run_gh_sync.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            assert _detect_repo_from_remote() is None


# ---------------------------------------------------------------------------
# ensure_repo_configured
# ---------------------------------------------------------------------------


class TestEnsureRepoConfigured:
    def test_returns_existing_repo(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: existing/repo\n")
        monkeypatch.chdir(tmp_path)

        repo = ensure_repo_configured()
        assert repo == "existing/repo"

    def test_auto_detects_and_writes_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("projectName: Test\n")
        monkeypatch.chdir(tmp_path)

        with patch(
            "ampa.run_gh_sync._detect_repo_from_remote", return_value="auto/detected"
        ):
            repo = ensure_repo_configured()

        assert repo == "auto/detected"
        # Verify the config file was updated (simple key: value format)
        from ampa.run_gh_sync import _read_config

        updated = _read_config(cfg_path)
        assert updated["githubRepo"] == "auto/detected"
        assert updated["projectName"] == "Test"

    def test_auto_detects_when_not_set(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text('githubRepo: "(not set)"\n')
        monkeypatch.chdir(tmp_path)

        with patch(
            "ampa.run_gh_sync._detect_repo_from_remote", return_value="owner/repo"
        ):
            repo = ensure_repo_configured()

        assert repo == "owner/repo"

    def test_returns_none_when_no_remote(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("projectName: Test\n")
        monkeypatch.chdir(tmp_path)

        with patch("ampa.run_gh_sync._detect_repo_from_remote", return_value=None):
            repo = ensure_repo_configured()

        assert repo is None

    def test_idempotent_config_update(self, tmp_path, monkeypatch):
        """Running ensure_repo_configured twice should not change the config."""
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("projectName: Test\n")
        monkeypatch.chdir(tmp_path)

        with patch(
            "ampa.run_gh_sync._detect_repo_from_remote", return_value="owner/repo"
        ):
            first = ensure_repo_configured()

        # Second call should read from config, not call detect again
        second = ensure_repo_configured()
        assert first == second == "owner/repo"

    def test_no_config_file(self, tmp_path, monkeypatch):
        """When no config file exists, auto-detect should still work and create config."""
        wl_dir = tmp_path / ".worklog"
        wl_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        with patch(
            "ampa.run_gh_sync._detect_repo_from_remote", return_value="owner/repo"
        ):
            repo = ensure_repo_configured()

        assert repo == "owner/repo"
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        assert cfg_path.exists()


# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------


class TestRunSync:
    def test_invalid_mode(self):
        assert run_sync("invalid") == 1

    def test_import_mode_success(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        result = subprocess.CompletedProcess(args=[], returncode=0)
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result) as mock_run:
            exit_code = run_sync("import")

        assert exit_code == 0
        mock_run.assert_called_once_with(
            ["wl", "github", "import", "--create-new", "--repo", "owner/repo"],
            timeout=300,
        )

    def test_push_mode_success(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        result = subprocess.CompletedProcess(args=[], returncode=0)
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result) as mock_run:
            exit_code = run_sync("push")

        assert exit_code == 0
        mock_run.assert_called_once_with(
            ["wl", "github", "push", "--repo", "owner/repo"],
            timeout=300,
        )

    def test_command_failure(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        result = subprocess.CompletedProcess(args=[], returncode=1)
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            exit_code = run_sync("import")

        assert exit_code == 1

    def test_command_timeout(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        with patch(
            "ampa.run_gh_sync.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="wl", timeout=300),
        ):
            exit_code = run_sync("import")

        assert exit_code == 1

    def test_no_repo_configured_and_no_remote(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("projectName: Test\n")
        monkeypatch.chdir(tmp_path)

        with patch("ampa.run_gh_sync._detect_repo_from_remote", return_value=None):
            exit_code = run_sync("import")

        assert exit_code == 1


# ---------------------------------------------------------------------------
# main (CLI entry point)
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_args(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["run_gh_sync"])
        assert main() == 1

    def test_invalid_mode(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["run_gh_sync", "invalid"])
        assert main() == 1

    def test_valid_import(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["run_gh_sync", "import"])
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        result = subprocess.CompletedProcess(args=[], returncode=0)
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert main() == 0

    def test_valid_push(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["run_gh_sync", "push"])
        cfg_path = tmp_path / ".worklog" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("githubRepo: owner/repo\n")
        monkeypatch.chdir(tmp_path)

        result = subprocess.CompletedProcess(args=[], returncode=0)
        with patch("ampa.run_gh_sync.subprocess.run", return_value=result):
            assert main() == 0


# ---------------------------------------------------------------------------
# Regex pattern tests
# ---------------------------------------------------------------------------


class TestGitHubUrlPatterns:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("git@github.com:owner/repo.git", ("owner", "repo")),
            ("git@github.com:owner/repo", ("owner", "repo")),
            ("git@github.com:Org-Name/my-repo.git", ("Org-Name", "my-repo")),
        ],
    )
    def test_ssh_pattern(self, url, expected):
        m = _GH_SSH_RE.match(url)
        assert m is not None
        assert (m.group("owner"), m.group("repo")) == expected

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/owner/repo.git", ("owner", "repo")),
            ("https://github.com/owner/repo", ("owner", "repo")),
            ("https://github.com/Org-Name/my-repo.git", ("Org-Name", "my-repo")),
        ],
    )
    def test_https_pattern(self, url, expected):
        m = _GH_HTTPS_RE.match(url)
        assert m is not None
        assert (m.group("owner"), m.group("repo")) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "git@gitlab.com:owner/repo.git",
            "https://gitlab.com/owner/repo.git",
            "not-a-url",
        ],
    )
    def test_non_github_urls_dont_match(self, url):
        assert _GH_SSH_RE.match(url) is None
        assert _GH_HTTPS_RE.match(url) is None
