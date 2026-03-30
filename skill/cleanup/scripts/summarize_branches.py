from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from skill.cleanup.scripts import lib


PROTECTED = {"main", "master", "develop"}


def list_local_branches(runner: lib.CommandRunner) -> list[str]:
    proc = runner.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    return [b.strip() for b in proc.stdout.splitlines() if b.strip()]


def has_remote(runner: lib.CommandRunner, branch: str) -> bool:
    return (
        runner.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"]
        ).returncode
        == 0
    )


def last_commit(runner: lib.CommandRunner, branch: str) -> dict[str, Any]:
    proc = runner.run(["git", "log", "-1", "--format=%H%x09%ci", branch])
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    sha, date = proc.stdout.strip().split("\t", 1)
    return {"sha": sha, "date": date}


def merged_into_default(
    runner: lib.CommandRunner, branch: str, default_ref: str
) -> bool:
    return (
        runner.run(
            ["git", "merge-base", "--is-ancestor", branch, default_ref]
        ).returncode
        == 0
    )


def parse_work_item(branch: str) -> tuple[str, str]:
    """Parse a work item token from branch name.

    Returns (token, work_item_id). The token is the matched token (e.g. WL-123).
    The work_item_id is the same token if the numeric suffix is digits, else empty.
    """
    m = re.search(r"([A-Za-z]+-\d+)", branch)
    if m:
        token = m.group(1)
        wid = token if token.rsplit("-", 1)[-1].isdigit() else ""
        return token, wid
    return "", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize local and remote branches with metadata"
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    default_branch = lib.parse_default_branch(runner, args.default)
    default_ref = lib.get_default_ref(runner, default_branch)

    branches = list_local_branches(runner)
    pr_report = None
    open_prs: list[dict[str, Any]] = []
    warning = ""
    if lib.ensure_tool_available("gh"):
        pr_report = runner.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--base",
                default_branch,
                "--json",
                "number,title,headRefName,url,author",
            ]
        )  # noqa: E501
        if pr_report.returncode == 0:
            open_prs = lib.parse_json_payload(pr_report.stdout) or []
    else:
        warning = "gh not available; cannot list PRs"

    open_pr_by_head = {
        pr.get("headRefName"): pr for pr in open_prs if pr.get("headRefName")
    }
    open_pr_heads = set(open_pr_by_head.keys())

    data = []
    for b in branches:
        entry = {"branch": b}
        entry["protected"] = b in PROTECTED
        entry["has_remote"] = has_remote(runner, b)
        entry["last_commit"] = last_commit(runner, b)
        entry["merged_into_default"] = merged_into_default(runner, b, default_ref)
        token, wid = parse_work_item(b)
        entry["work_item_token"] = token
        entry["work_item_id"] = wid
        entry["open_pr"] = b in open_pr_heads
        entry["open_pr_info"] = open_pr_by_head.get(b)
        data.append(entry)

    report = {
        "operation": "summarize_branches",
        "default_branch": default_branch,
        "warning": warning,
        "open_prs": open_prs,
        "branches": data,
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
