import json

from scripts.cleanup import prune_local_branches
from scripts.cleanup import cleanup_stale_remote_branches
from scripts.cleanup import lib


class DummyRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, cmd):
        key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        self.calls.append(key)
        return self.responses.get(key, lib.CommandResult(cmd, 0, "", ""))


def test_prune_local_branches_dry_run(tmp_path, monkeypatch):
    runner = DummyRunner(
        {
            "git rev-parse --abbrev-ref HEAD": lib.CommandResult(
                [], 0, "feature/test", ""
            ),
            "git for-each-ref --format=%(refname:short) refs/heads/": lib.CommandResult(
                [], 0, "main\nfeature/test\nfeature/old\n", ""
            ),
            "git merge-base --is-ancestor feature/old main": lib.CommandResult(
                [], 0, "", ""
            ),
            "git show-ref --verify --quiet refs/heads/main": lib.CommandResult(
                [], 0, "", ""
            ),
        }
    )
    monkeypatch.setattr(prune_local_branches, "lib", lib)
    monkeypatch.setattr(lib, "CommandRunner", lambda: runner)
    monkeypatch.setattr(lib, "ensure_tool_available", lambda tool: True)

    report_path = tmp_path / "local.json"
    exit_code = prune_local_branches.main(["--dry-run", "--report", str(report_path)])
    assert exit_code == 0
    payload = json.loads(report_path.read_text())
    assert payload["operation"] == "prune_local_branches"
    assert payload["dry_run"] is True


def test_cleanup_stale_remote_branches_dry_run(tmp_path, monkeypatch):
    runner = DummyRunner(
        {
            "git remote show origin": lib.CommandResult([], 0, "HEAD branch: main", ""),
            "git show-ref --verify --quiet refs/remotes/origin/main": lib.CommandResult(
                [], 0, "", ""
            ),
            "git for-each-ref --format=%(refname:short)\t%(committerdate:iso8601) refs/remotes/origin/": lib.CommandResult(
                [],
                0,
                "origin/old\t2023-01-01 00:00:00 +0000\n",
                "",
            ),
            "git merge-base --is-ancestor origin/old origin/main": lib.CommandResult(
                [], 0, "", ""
            ),
        }
    )
    monkeypatch.setattr(cleanup_stale_remote_branches, "lib", lib)
    monkeypatch.setattr(lib, "CommandRunner", lambda: runner)
    monkeypatch.setattr(lib, "ensure_tool_available", lambda tool: True)

    report_path = tmp_path / "remote.json"
    exit_code = cleanup_stale_remote_branches.main(
        ["--dry-run", "--days", "1", "--report", str(report_path)]
    )
    assert exit_code == 0
    payload = json.loads(report_path.read_text())
    assert payload["operation"] == "cleanup_stale_remote_branches"
    assert payload["dry_run"] is True


