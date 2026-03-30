from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# When this script is executed directly (for example in CI as
# `python scripts/cleanup/prune_local_branches.py`) Python's
# import search path may be `scripts/cleanup/` which prevents
# importing the top-level `scripts` package. Ensure the repo
# root is on `sys.path` so `from scripts.cleanup import lib`
# works consistently both locally and in CI.
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scripts.cleanup import lib


PROTECTED_BRANCHES = {"main", "master", "develop"}


def parse_branch_list(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def is_merged(runner: lib.CommandRunner, branch: str, default_ref: str) -> bool:
    proc = runner.run(["git", "merge-base", "--is-ancestor", branch, default_ref])
    return proc.returncode == 0


def get_current_branch(runner: lib.CommandRunner) -> str:
    proc = runner.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune local branches merged into the default branch."
    )
    lib.add_common_args(parser)
    parser.add_argument(
        "--branches-file",
        help="Path to JSON (or newline-separated) file listing branches to consider for deletion",
    )
    parser.add_argument("--default", help="Override default branch name")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch and prune remote tracking branches before scanning",
    )
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    if args.fetch:
        lib.run_command(
            ["git", "fetch", "origin", "--prune"],
            dry_run=args.dry_run,
            destructive=False,
            runner=runner,
        )

    default_branch = lib.get_default_branch(runner, args.default)
    default_ref = lib.get_default_ref(runner, default_branch)
    current_branch = get_current_branch(runner)

    list_proc = runner.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    branches = parse_branch_list(list_proc.stdout)

    # If a branches-file was provided, use it to limit the set of branches we consider.
    if args.branches_file:
        try:
            with open(args.branches_file, "r", encoding="utf-8") as fh:
                payload = fh.read()
        except FileNotFoundError:
            lib.exit_with_error(f"branches file not found: {args.branches_file}")

        # Try JSON first, fall back to newline-separated list
        parsed: list[str] | None = None
        try:
            obj = json.loads(payload)
            if isinstance(obj, list):
                parsed = [str(x) for x in obj]
            elif isinstance(obj, dict):
                for key in ("branches", "items", "branch_list"):
                    if key in obj and isinstance(obj[key], list):
                        parsed = [str(x) for x in obj[key]]
                        break
        except json.JSONDecodeError:
            parsed = None

        if parsed is None:
            parsed = [line.strip() for line in payload.splitlines() if line.strip()]

        # Keep only branches that actually exist locally
        branches = [b for b in branches if b in set(parsed)]

    actions: list[dict[str, Any]] = []
    for branch in branches:
        if branch in PROTECTED_BRANCHES:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "protected",
                }
            )
            continue
        if branch == current_branch:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "current",
                }
            )
            continue
        merged = is_merged(runner, branch, default_ref)
        if not merged:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "not_merged",
                }
            )
            continue
        if not lib.confirm_action(
            f"Delete local branch '{branch}'?", args.yes, args.dry_run
        ):
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "declined",
                }
            )
            continue
        proc = lib.run_command(
            ["git", "branch", "-d", branch],
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
        "operation": "prune_local_branches",
        "default_branch": default_branch,
        "dry_run": args.dry_run,
        "actions": actions,
        "summary": lib.render_summary(actions),
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
