from __future__ import annotations

import argparse
from datetime import timedelta
import os
import sys
from typing import Any


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from skill.cleanup.scripts import lib


PROTECTED = {"main", "master", "develop"}


def is_merged_remote(runner: lib.CommandRunner, branch: str, default_ref: str) -> bool:
    proc = runner.run(
        ["git", "merge-base", "--is-ancestor", f"origin/{branch}", default_ref]
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete remote branches that meet criteria"
    )
    lib.add_common_args(parser)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    default_branch = lib.parse_default_branch(runner, args.default)
    # fetch
    fetch_proc = runner.run(["git", "fetch", "origin", "--prune"])
    default_ref = lib.get_default_ref(runner, default_branch)

    # list remote branches
    list_proc = runner.run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)\t%(committerdate:iso8601)",
            "refs/remotes/origin/",
        ]
    )

    branches = []
    for line in list_proc.stdout.splitlines():
        if not line.strip() or "origin/HEAD" in line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name = parts[0].replace("origin/", "", 1)
        branches.append((name, parts[1]))

    actions: list[dict[str, Any]] = []
    for name, date_str in branches:
        if name in PROTECTED:
            actions.append({"branch": name, "action": "skip", "result": "protected"})
            continue
        commit_time = lib.parse_iso_datetime(date_str)
        if commit_time is None:
            actions.append({"branch": name, "action": "skip", "result": "unknown_date"})
            continue
        threshold = lib.utc_now() - timedelta(days=args.days)
        if commit_time > threshold:
            actions.append({"branch": name, "action": "skip", "result": "recent"})
            continue
        merged = is_merged_remote(runner, name, default_ref)
        if not merged:
            actions.append({"branch": name, "action": "skip", "result": "not_merged"})
            continue
        # avoid deleting if PR is open
        if lib.ensure_tool_available("gh"):
            pr_proc = runner.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "open",
                    "--head",
                    name,
                    "--json",
                    "number",
                ]
            )
            if pr_proc.returncode == 0 and lib.parse_json_payload(pr_proc.stdout):
                actions.append({"branch": name, "action": "skip", "result": "open_pr"})
                continue

        proc = lib.run_command(
            ["git", "push", "origin", "--delete", name],
            dry_run=args.dry_run,
            destructive=True,
            runner=runner,
        )
        actions.append(
            {
                "branch": name,
                "action": "delete",
                "result": "deleted" if proc.returncode == 0 else "failed",
            }
        )

    report = {
        "operation": "delete_remote_branches",
        "default_branch": default_branch,
        "default_ref": default_ref,
        "dry_run": args.dry_run,
        "threshold_days": args.days,
        "fetch": {
            "returncode": fetch_proc.returncode,
            "stderr": fetch_proc.stderr.strip(),
        },
        "actions": actions,
        "summary": lib.render_summary(actions),
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
