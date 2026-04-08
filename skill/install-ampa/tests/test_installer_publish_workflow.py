import os
import json
import tempfile
import shutil
import subprocess

import pytest

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

    proc = run_installer(["--yes", "--force-workflow", "--docs-path", str(workflow), 
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

    proc = run_installer(["--yes", "--force-workflow", 
                          os.path.join(os.path.dirname(INSTALLER), "../resources/ampa.mjs"), 
                          os.path.join(xdg_config_home, "opencode/.worklog/plugins")], env=env)

    assert proc.returncode == 0, proc.stderr

    dest = xdg / "opencode" / ".worklog" / "ampa" / "workflow.json"
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert "version" in data
