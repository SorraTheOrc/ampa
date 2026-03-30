"""Integration-style test for the triage helper using a fake `wl` CLI.

This test runs the actual script via a subprocess and provides a small
fake `wl` executable placed on PATH that reads/modifies a JSON state file
to emulate `wl list`/`wl create`/`wl comment` behaviour.

It verifies:
- a first run creates a new issue and emits a `triage.issue.created` event
- a second run finds the created issue and enhances it (no new create)
"""

import json
import os
import subprocess
import sys
from datetime import datetime


def _write_wl_state(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def test_integration_create_then_match(tmp_path, monkeypatch):
    repo_root = os.getcwd()

    # Setup a small state file the fake `wl` will use
    state_file = tmp_path / "wl_state.json"
    initial_state = {"list": [], "created_id": "SA-INTEG"}
    _write_wl_state(state_file, initial_state)

    # Create a fake `wl` executable script
    wl_script = tmp_path / "wl"
    wl_script.write_text(
        """#!/usr/bin/env python3
import json,sys,os
state_path = os.path.join(os.path.dirname(__file__), 'wl_state.json')
with open(state_path, 'r', encoding='utf-8') as fh:
    state = json.load(fh)
args = sys.argv[1:]
if not args:
    print('{}')
    sys.exit(0)
cmd = args[0]
if cmd == 'list':
    print(json.dumps(state.get('list', [])))
    sys.exit(0)
if cmd == 'create':
    # create returns an id and appends a representative item to list
    cid = state.get('created_id', 'SA-UNKNOWN')
    created = {'id': cid}
    # add a simple list item the next time `list` is called
    now = datetime = __import__('datetime').datetime
    item = {
        'id': cid,
        'title': '[test-failure] test_integ',
        'description': 'Test name: test_integ',
        'status': 'open',
        'updatedAt': now.utcnow().isoformat() + 'Z',
    }
    state.setdefault('list', []).append(item)
    with open(state_path, 'w', encoding='utf-8') as fh:
        json.dump(state, fh)
    print(json.dumps(created))
    sys.exit(0)
if cmd == 'comment':
    # emulate adding a comment
    print(json.dumps({}))
    sys.exit(0)
print('{}')
""",
        encoding="utf-8",
    )
    # Make it executable
    wl_script.chmod(0o755)

    # Use the real python executable to run the triage script
    triage_script = os.path.join(repo_root, "skill/triage/scripts/check_or_create.py")

    env = os.environ.copy()
    # Prepend tmp_path to PATH so our fake `wl` is picked up
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    payload = {"test_name": "test_integ", "stdout_excerpt": "failure output"}

    # First run: should create
    proc = subprocess.run(
        [sys.executable, triage_script, json.dumps(payload)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, f"triage script failed: {proc.stderr}"
    out = json.loads(proc.stdout)
    assert out.get("created") is True
    assert out.get("issueId") == "SA-INTEG"
    # telemetry event should be emitted to stderr
    assert "triage.issue.created" in proc.stderr

    # Second run: should find the created item and not create a new one
    proc2 = subprocess.run(
        [sys.executable, triage_script, json.dumps(payload)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc2.returncode == 0, f"second run failed: {proc2.stderr}"
    out2 = json.loads(proc2.stdout)
    assert out2.get("created") is False
    assert out2.get("matchedId") == "SA-INTEG"
    assert "triage.issue.enhanced" in proc2.stderr
