import json
import subprocess
import sys


def test_cleanup_scripts_dry_run_cli(tmp_path):
    """Run the cleanup scripts via the same CLI the workflow uses in dry-run.

    This integration-style test ensures the top-level script entrypoints accept
    the workflow flags (e.g. --branches-file) and produce JSON reports. It
    uses the repository's Python executable so CI will exercise the same
    interpreter used by the workflow.
    """
    branches_file = tmp_path / "branches_to_delete.json"
    branches_file.write_text("[]\n")

    local_report = tmp_path / "local.json"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/cleanup/prune_local_branches.py",
            "--dry-run",
            "--branches-file",
            str(branches_file),
            "--report",
            str(local_report),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert local_report.exists(), proc.stdout + proc.stderr
    payload = json.loads(local_report.read_text())
    assert payload.get("operation") == "prune_local_branches"
    assert payload.get("dry_run") is True

    remote_report = tmp_path / "remote.json"
    proc2 = subprocess.run(
        [
            sys.executable,
            "scripts/cleanup/cleanup_stale_remote_branches.py",
            "--dry-run",
            "--days",
            "1",
            "--report",
            str(remote_report),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remote_report.exists(), proc2.stdout + proc2.stderr
    payload2 = json.loads(remote_report.read_text())
    assert payload2.get("operation") == "cleanup_stale_remote_branches"
    assert payload2.get("dry_run") is True
