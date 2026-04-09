import os
import json
import tempfile
import shutil
import subprocess

import pytest
from pathlib import Path

INSTALLER = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worklog-plugin.sh")


def run_installer(args, env=None, cwd=None):
    cmd = [INSTALLER] + args
    proc = subprocess.run(["sh"] + cmd, env=env, cwd=cwd, capture_output=True, text=True)
    return proc


def test_publish_with_explicit_docs_path(tmp_path, monkeypatch):
    # Create a fake docs/workflow/workflow.json and invoke installer with --docs-path
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    docs_dir = repo_root / "docs" / "workflow"
    docs_dir.mkdir(parents=True, exist_ok=True)
    workflow = docs_dir / "workflow.json"
    workflow.write_text(json.dumps({"version": "1.0.0"}))

    xdg = tmp_path / "xdg"
    xdg_config_home = str(xdg)

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = xdg_config_home

    proc = run_installer(["--yes", "--force-workflow", "--no-restart", "--docs-path", str(workflow), 
                          os.path.join(os.path.dirname(INSTALLER), "../resources/ampa.mjs"), 
                          os.path.join(xdg_config_home, "opencode/.worklog/plugins")], env=env)

    assert proc.returncode == 0, proc.stderr

    # Verify XDG workflow was published
    dest = xdg / "opencode" / ".worklog" / "ampa" / "workflow.json"
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert data.get("version") == "1.0.0"


def test_publish_falls_back_to_bundled(tmp_path, monkeypatch):
    # Simulate installer run where no repo docs present; expect bundled resource to be used
    xdg = tmp_path / "xdg2"
    xdg_config_home = str(xdg)

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = xdg_config_home

    proc = run_installer(["--yes", "--force-workflow", "--no-restart", 
                          os.path.join(os.path.dirname(INSTALLER), "../resources/ampa.mjs"), 
                          os.path.join(xdg_config_home, "opencode/.worklog/plugins")], env=env)

    assert proc.returncode == 0, proc.stderr

    dest = xdg / "opencode" / ".worklog" / "ampa" / "workflow.json"
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert "version" in data


def test_env_preserved_across_upgrade(tmp_path, monkeypatch):
    """Simulate a project-local install where .worklog/plugins/ampa_py/ampa is
    upgraded and ensure .worklog/ampa/.env is preserved/restored from backups.
    """
    # Setup fake project layout
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".worklog" / "plugins").mkdir(parents=True)
    # Create an initial project env file that should be preserved
    project_env_dir = project / ".worklog" / "ampa"
    project_env_dir.mkdir(parents=True)
    env_file = project_env_dir / ".env"
    env_file.write_text("AMPA_DISCORD_BOT_TOKEN=original-token\nAMPA_DISCORD_CHANNEL_ID=chan123\n")

    # Create a dummy existing python package that will be removed by installer
    py_target = project / ".worklog" / "plugins" / "ampa_py" / "ampa"
    py_target.parent.mkdir(parents=True, exist_ok=True)
    py_target.mkdir(parents=True, exist_ok=True)
    (py_target / "scheduler_store.json").write_text('{}')

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")

    # Run installer in project directory, forcing local install to project plugins
    proc = run_installer(["--yes", "--no-restart", "--local", os.path.join(os.path.dirname(INSTALLER), "../resources/ampa.mjs")], env=env, cwd=str(project))
    assert proc.returncode == 0, proc.stderr

    # Verify project .env still exists and contains the original token
    final_env = project / ".worklog" / "ampa" / ".env"
    assert final_env.exists(), final_env
    content = final_env.read_text()
    assert "original-token" in content
