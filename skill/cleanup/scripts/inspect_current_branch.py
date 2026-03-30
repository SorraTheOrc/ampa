from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from skill.cleanup.scripts import lib


def get_unpushed_count(runner: lib.CommandRunner, branch: str) -> int:
    if not branch:
        return 0
    proc = runner.run(
        [
            "git",
            "rev-list",
            "--count",
            f"refs/remotes/origin/{branch}..refs/heads/{branch}",
        ]
    )
    try:
        return int(proc.stdout.strip() or 0)
    except Exception:
        return 0


def get_last_commit(branch: str, runner: lib.CommandRunner) -> dict[str, Any]:
    proc = runner.run(["git", "log", "-1", "--format=%H%x09%ci%x09%an", branch])
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    parts = proc.stdout.strip().split("\t")
    return (
        {"sha": parts[0], "date": parts[1], "author": parts[2]}
        if len(parts) >= 3
        else {}
    )


def get_uncommitted_changes(runner: lib.CommandRunner) -> dict[str, Any]:
    proc = runner.run(["git", "status", "--porcelain"])
    if proc.returncode != 0:
        return {"present": False, "files": []}
    files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return {"present": bool(files), "files": files}


def inspect_current_branch(
    runner: lib.CommandRunner,
    default_override: str | None,
) -> dict[str, Any]:
    default_branch = lib.parse_default_branch(runner, default_override)
    current_branch = runner.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).stdout.strip()

    fetch_result: dict[str, Any] = {"attempted": False}
    if current_branch and current_branch != default_branch:
        proc = runner.run(["git", "fetch", "origin", "--prune"])
        fetch_result = {
            "attempted": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    default_ref = lib.get_default_ref(runner, default_branch)

    merged = False
    if current_branch and default_ref:
        merged = (
            runner.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", default_ref]
            ).returncode
            == 0
        )

    last_commit = get_last_commit(current_branch, runner)
    unpushed = get_unpushed_count(runner, current_branch)
    uncommitted = get_uncommitted_changes(runner)

    # parse work item token from branch name
    token = ""
    wid = ""
    import re

    m = re.search(r"([A-Za-z]+-[0-9]+)", current_branch or "")
    if m:
        token = m.group(1)
        if token.rsplit("-", 1)[-1].isdigit():
            wid = token

    requires_interaction = False
    recommended_action = "continue"
    interactive_prompt = ""
    if current_branch and current_branch != default_branch:
        if merged:
            recommended_action = "switch_to_default"
        else:
            requires_interaction = True
            recommended_action = "prompt_user"
            interactive_prompt = (
                "Current branch is not merged into default. "
                "Choose: keep working / open PR / merge / skip deletion."
            )

    return {
        "current_branch": current_branch,
        "default_branch": default_branch,
        "fetch": fetch_result,
        "merged_into_default": merged,
        "last_commit": last_commit,
        "uncommitted_changes": uncommitted,
        "unpushed_commits": unpushed,
        "work_item_token": token,
        "work_item_id": wid,
        "requires_interaction": requires_interaction,
        "recommended_action": recommended_action,
        "interactive_prompt": interactive_prompt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect current branch and default branch status"
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    result = inspect_current_branch(runner, args.default)
    lib.write_report(result, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
