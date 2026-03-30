from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


LOG = logging.getLogger("cleanup")


@dataclass
class CommandResult:
    args: Sequence[str] | str
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(self, cmd: Sequence[str] | str) -> CommandResult:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(
            args=proc.args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def run_command(
    cmd: Sequence[str] | str,
    *,
    dry_run: bool,
    destructive: bool,
    runner: CommandRunner,
) -> CommandResult:
    if dry_run and destructive:
        LOG.info("dry-run: skipping %s", cmd)
        return CommandResult(args=cmd, returncode=0, stdout="", stderr="")
    return runner.run(cmd)


def ensure_tool_available(tool: str) -> bool:
    return shutil.which(tool) is not None


def confirm_action(prompt: str, assume_yes: bool, dry_run: bool) -> bool:
    if dry_run or assume_yes:
        return True
    reply = input(f"{prompt} [y/N]: ").strip().lower()
    return reply in {"y", "yes"}


def write_report(
    report: dict[str, Any],
    path: str | None,
    *,
    print_output: bool = True,
) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True)
    if path:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    if print_output:
        print(payload)


def parse_json_payload(payload: str) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def normalize_items(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "workItems", "work_items"):
            if key in payload and isinstance(payload[key], list):
                return [item for item in payload[key] if isinstance(item, dict)]
    return []


def parse_default_branch(remote_show: str) -> str | None:
    for line in remote_show.splitlines():
        if "HEAD branch" in line:
            return line.split(":", 1)[-1].strip()
    return None


def get_default_branch(runner: CommandRunner, override: str | None) -> str:
    if override:
        return override
    if ensure_tool_available("git"):
        proc = runner.run(["git", "remote", "show", "origin"])
        if proc.returncode == 0:
            parsed = parse_default_branch(proc.stdout)
            if parsed:
                return parsed
        proc = runner.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
        if proc.returncode == 0:
            return proc.stdout.strip().split("/", 3)[-1]
    return "main"


def ref_exists(runner: CommandRunner, ref: str) -> bool:
    proc = runner.run(["git", "show-ref", "--verify", "--quiet", ref])
    return proc.returncode == 0


def get_default_ref(runner: CommandRunner, default_branch: str) -> str:
    remote_ref = f"refs/remotes/origin/{default_branch}"
    local_ref = f"refs/heads/{default_branch}"
    if ref_exists(runner, remote_ref):
        return f"origin/{default_branch}"
    if ref_exists(runner, local_ref):
        return default_branch
    return default_branch


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Do not make changes")
    parser.add_argument("--yes", action="store_true", help="Assume yes for prompts")
    parser.add_argument("--report", help="Write JSON report to path")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress JSON report output to stdout",
    )
    parser.add_argument(
        "--verbose",
        action="count",
        default=0,
        help="Increase logging verbosity",
    )


def render_summary(actions: Iterable[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for action in actions:
        key = action.get("result", "unknown")
        summary[key] = summary.get(key, 0) + 1
    return summary


def exit_with_error(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)
