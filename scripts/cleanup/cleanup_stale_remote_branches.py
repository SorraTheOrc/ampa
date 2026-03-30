from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from typing import Any

# Ensure repository root is on sys.path so `scripts.cleanup` imports
# work when the script is executed directly in CI or by tests.
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scripts.cleanup import lib


PROTECTED_BRANCHES = {"main", "master", "develop"}


def parse_remote_branches(output: str) -> list[tuple[str, str]]:
    branches: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.startswith("origin/HEAD"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        branch, date = parts
        name = branch.replace("origin/", "", 1)
        branches.append((name, date))
    return branches


def is_merged_remote(runner: lib.CommandRunner, branch: str, default_ref: str) -> bool:
    proc = runner.run(
        ["git", "merge-base", "--is-ancestor", f"origin/{branch}", default_ref]
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List or delete stale remote branches."
    )
    lib.add_common_args(parser)
    parser.add_argument("--days", type=int, default=30, help="Age threshold in days")
    parser.add_argument("--default", help="Override default branch name")
    parser.add_argument(
        "--include-unmerged",
        action="store_true",
        help="Include branches not merged into default in report",
    )
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    lib.run_command(
        ["git", "fetch", "origin", "--prune"],
        dry_run=args.dry_run,
        destructive=False,
        runner=runner,
    )

    default_branch = lib.get_default_branch(runner, args.default)
    default_ref = lib.get_default_ref(runner, default_branch)

    list_proc = runner.run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)\t%(committerdate:iso8601)",
            "refs/remotes/origin/",
        ]
    )
    branches = parse_remote_branches(list_proc.stdout)
    threshold = lib.utc_now() - timedelta(days=args.days)

    actions: list[dict[str, Any]] = []
    for branch, date_str in branches:
        if branch in PROTECTED_BRANCHES:
            actions.append({"branch": branch, "action": "skip", "result": "protected"})
            continue
        commit_time = lib.parse_iso_datetime(date_str)
        if commit_time is None:
            actions.append(
                {"branch": branch, "action": "skip", "result": "unknown_date"}
            )
            continue
        if commit_time > threshold:
            actions.append({"branch": branch, "action": "skip", "result": "recent"})
            continue
        merged = is_merged_remote(runner, branch, default_ref)
        if not merged and not args.include_unmerged:
            actions.append({"branch": branch, "action": "skip", "result": "not_merged"})
            continue
        if not lib.confirm_action(
            f"Delete remote branch '{branch}'?", args.yes, args.dry_run
        ):
            actions.append({"branch": branch, "action": "skip", "result": "declined"})
            continue
        proc = lib.run_command(
            ["git", "push", "origin", "--delete", branch],
            dry_run=args.dry_run,
            destructive=True,
            runner=runner,
        )
        actions.append(
            {
                "branch": branch,
                "action": "delete",
                "result": "deleted" if proc.returncode == 0 else "failed",
                "stderr": proc.stderr.strip(),
            }
        )

    report = {
        "operation": "cleanup_stale_remote_branches",
        "default_branch": default_branch,
        "dry_run": args.dry_run,
        "threshold_days": args.days,
        "actions": actions,
        "summary": lib.render_summary(actions),
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
