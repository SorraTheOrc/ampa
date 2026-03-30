from __future__ import annotations

import argparse
import os
import sys
from typing import Any


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from skill.cleanup.scripts import lib


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Switch to default branch and update from origin"
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    default_branch = lib.parse_default_branch(runner, args.default)

    actions = []
    # fetch
    proc_fetch = runner.run(["git", "fetch", "origin", "--prune"])
    actions.append(
        {
            "action": "fetch",
            "rc": proc_fetch.returncode,
            "stderr": proc_fetch.stderr.strip(),
        }
    )

    # checkout
    proc_co = runner.run(["git", "checkout", default_branch])
    actions.append(
        {
            "action": "checkout",
            "rc": proc_co.returncode,
            "stderr": proc_co.stderr.strip(),
        }
    )

    # pull --ff-only
    proc_pull = runner.run(["git", "pull", "--ff-only", "origin", default_branch])
    actions.append(
        {
            "action": "pull",
            "rc": proc_pull.returncode,
            "stderr": proc_pull.stderr.strip(),
        }
    )

    report = {
        "operation": "switch_to_default_and_update",
        "default_branch": default_branch,
        "actions": actions,
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
