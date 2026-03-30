"""CLI layer for AMPA scheduler (extracted from ampa/scheduler.py).

This module contains only presentation-layer code: argument parsing,
CLI handlers and output formatting. It imports scheduling types and
helpers from ``ampa.scheduler`` to avoid duplicating core logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import datetime as dt
import re
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from . import daemon, notifications as notifications_module, selection
from .scheduler_types import (
    CommandSpec,
    SchedulerConfig,
    RunResult,
    CommandRunResult,
    _utc_now,
    _from_iso,
    _to_iso,
)
from .scheduler_store import SchedulerStore
from .scheduler import (
    load_scheduler,
    build_error_report,
    render_error_report,
    render_error_report_json,
)

LOG = logging.getLogger("ampa.scheduler.cli")

# ---------------------------------------------------------------------------
# Daemon detection and delegation helpers
# ---------------------------------------------------------------------------

def _daemon_port() -> int:
    """Return the HTTP port of the running daemon (from AMPA_METRICS_PORT)."""
    try:
        return int(os.getenv("AMPA_METRICS_PORT", "8000"))
    except Exception:
        return 8000


def _try_daemon_run(command_id: str) -> Optional[Dict[str, Any]]:
    """Try to execute a command via the running daemon's /run endpoint.

    If the daemon is available and has a scheduler registered it will execute
    the command and return the result as a dict (same schema as
    ``_format_run_result_json``).  Returns ``None`` if the daemon is not
    reachable, has no scheduler running, or the command is unknown.
    """
    port = _daemon_port()
    if port <= 0:
        return None
    url = f"http://127.0.0.1:{port}/run"
    data = json.dumps({"command_id": command_id}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        # Allow up to AMPA_CMD_TIMEOUT_SECONDS for the command to complete.
        try:
            timeout = int(os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "3600"))
        except Exception:
            timeout = 3600
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            if isinstance(result, dict):
                return result
            return None
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 503):
            # 404 = unknown command id, 503 = no scheduler running — let caller
            # fall back to local execution.
            LOG.debug(
                "Daemon /run returned %s for command %s; falling back to local",
                exc.code,
                command_id,
            )
        else:
            LOG.debug("Daemon /run HTTP error %s; falling back to local", exc.code)
        return None
    except Exception:
        LOG.debug("Daemon not reachable; falling back to local execution")
        return None


def _parse_metadata(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    raise ValueError("metadata must be a JSON object")


def _store_from_env() -> SchedulerStore:
    config = SchedulerConfig.from_env()
    return SchedulerStore(config.store_path)


def _command_description(spec: CommandSpec) -> str:
    meta = spec.metadata if isinstance(spec.metadata, dict) else {}
    desc = meta.get("description") if isinstance(meta, dict) else None
    if desc:
        return str(desc)
    if spec.command:
        return str(spec.command)
    if spec.command_type:
        return str(spec.command_type)
    return ""


def _build_command_listing(
    store: SchedulerStore, now: Optional[dt.datetime] = None
) -> List[Dict[str, Any]]:
    now = now or _utc_now()
    rows: List[Dict[str, Any]] = []
    for spec in store.list_commands():
        state = store.get_state(spec.command_id)
        last_run = _from_iso(state.get("last_run_ts"))
        next_run: Optional[dt.datetime] = None
        if last_run is not None and spec.frequency_minutes > 0:
            next_run = last_run + dt.timedelta(minutes=spec.frequency_minutes)
        rows.append(
            {
                "id": spec.command_id,
                "name": spec.title or spec.command_id,
                "description": _command_description(spec),
                "last_run": _to_iso(last_run),
                "next_run": _to_iso(next_run),
            }
        )
    rows.sort(key=lambda row: row.get("id") or "")
    return rows


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _format_command_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No commands configured."

    headers = ["id", "name", "description", "last_run", "next_run"]
    formatted: List[List[str]] = []
    for row in rows:
        description = _truncate_text(str(row.get("description") or ""), 60)
        last_run = row.get("last_run") or "never"
        next_run = row.get("next_run") or "n/a"
        if last_run not in ("never", None):
            parsed = _from_iso(str(last_run))
            if parsed is not None:
                last_run = parsed.astimezone().strftime("%d-%b-%Y %H:%M")
        if next_run not in ("n/a", None):
            parsed = _from_iso(str(next_run))
            if parsed is not None:
                next_run = parsed.astimezone().strftime("%d-%b-%Y %H:%M")
        formatted.append(
            [
                str(row.get("id") or ""),
                str(row.get("name") or ""),
                description,
                str(last_run),
                str(next_run),
            ]
        )

    widths = [len(h) for h in headers]
    for row in formatted:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    lines = ["  ".join(h.ljust(widths[idx]) for idx, h in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in formatted:
        lines.append(
            "  ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers)))
        )
    return "\n".join(lines)


def _cli_list(args: argparse.Namespace) -> int:
    store = _store_from_env()
    rows = _build_command_listing(store)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(_format_command_table(rows))
    return 0


def _cli_add(args: argparse.Namespace) -> int:
    store = _store_from_env()
    spec = CommandSpec(
        command_id=args.command_id,
        command=args.command,
        title=getattr(args, "title", None),
        requires_llm=args.requires_llm,
        frequency_minutes=args.frequency_minutes,
        priority=args.priority,
        metadata=_parse_metadata(args.metadata),
        max_runtime_minutes=args.max_runtime_minutes,
        command_type=args.command_type,
    )
    store.add_command(spec)
    return 0


def _cli_update(args: argparse.Namespace) -> int:
    store = _store_from_env()
    spec = CommandSpec(
        command_id=args.command_id,
        command=args.command,
        title=getattr(args, "title", None),
        requires_llm=args.requires_llm,
        frequency_minutes=args.frequency_minutes,
        priority=args.priority,
        metadata=_parse_metadata(args.metadata),
        max_runtime_minutes=args.max_runtime_minutes,
        command_type=args.command_type,
    )
    store.update_command(spec)
    return 0


def _cli_remove(args: argparse.Namespace) -> int:
    store = _store_from_env()
    store.remove_command(args.command_id)
    return 0


def _cli_run_once(args: argparse.Namespace) -> int:
    """Legacy run-once handler -- delegates to _cli_run."""
    return _cli_run(args)


# ---------------------------------------------------------------------------
# Delegation helpers — canonical implementations live in ampa.delegation.
# Imported here to avoid duplicating code that was previously inlined.
# ---------------------------------------------------------------------------
from .delegation import (  # noqa: E402
    _trim_text,
    _build_delegation_report,
    _build_delegation_discord_message,
)


def _cli_dry_run(args: argparse.Namespace) -> int:
    scheduler = load_scheduler(command_cwd=os.getcwd())
    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation Report",
        command_type="delegation",
    )
    if args.discord and not os.getenv("AMPA_DISCORD_BOT_TOKEN"):
        LOG.warning("AMPA_DISCORD_BOT_TOKEN not set; discord flag will be ignored")
    report = scheduler._delegation_orchestrator.run_delegation_report(spec)
    if report:
        print(report)
        if args.discord:
            try:
                # Only send a Discord notification when the report content has
                # changed since the last time we posted. Use the orchestrator's
                # dedup helper which persists a content hash in the scheduler
                # state to suppress duplicate posts.
                if scheduler._delegation_orchestrator._is_delegation_report_changed(
                    spec.command_id, report
                ):
                    message = _build_delegation_discord_message(report)
                    notifications_module.notify(
                        "Delegation Report",
                        message,
                        message_type="command",
                    )
                else:
                    LOG.info("Delegation report unchanged; skipping discord notification")
            except Exception:
                LOG.exception("Failed to send delegation discord notification")
    return 0


def _format_run_result_json(spec: CommandSpec, run: RunResult, instance: str) -> str:
    output: Optional[str] = None
    if isinstance(run, CommandRunResult):
        output = run.output
    data = {
        "id": spec.command_id,
        "name": spec.title or spec.command_id,
        "status": "success" if run.exit_code == 0 else "failed",
        "started_at": _to_iso(run.start_ts),
        "finished_at": _to_iso(run.end_ts),
        "duration_seconds": round(run.duration_seconds, 3),
        "exit_code": run.exit_code,
        "output": output,
        "instance": instance,
    }
    if run.metadata and isinstance(run.metadata.get("delegation"), dict):
        deleg = run.metadata["delegation"]
        delegation_data: Dict[str, Any] = {
            "dispatched": deleg.get("dispatched", False),
            "note": deleg.get("note"),
        }
        if deleg.get("dispatched") and deleg.get("delegate_info"):
            info = deleg["delegate_info"]
            delegation_data["action"] = info.get("action")
            delegation_data["work_item_id"] = info.get("id")
            delegation_data["work_item_title"] = info.get("title")
        if deleg.get("rejected"):
            delegation_data["rejected_count"] = len(deleg["rejected"])
        data["delegation"] = delegation_data
    if run.metadata and isinstance(run.metadata.get("pr_monitor"), dict):
        data["pr_monitor"] = run.metadata["pr_monitor"]
    return json.dumps(data, indent=2, sort_keys=True)


def _format_run_result_human(
    spec: CommandSpec, run: RunResult, fmt: str, instance: str
) -> str:
    output: Optional[str] = None
    if isinstance(run, CommandRunResult):
        output = run.output

    if fmt == "raw":
        return output or ""

    if fmt == "concise":
        status = "OK" if run.exit_code == 0 else f"FAIL({run.exit_code})"
        duration = f"{run.duration_seconds:.3f}s"
        line = f"{spec.command_id}  {status}  {duration}"
        if run.metadata and isinstance(run.metadata.get("delegation"), dict):
            deleg = run.metadata["delegation"]
            if deleg.get("dispatched"):
                info = deleg.get("delegate_info") or {}
                line += f"  -> {info.get('action', '?')} {info.get('id', '?')}"
            elif deleg.get("note"):
                line += f"  [{deleg['note']}]"
        return line

    lines: List[str] = []
    status = "success" if run.exit_code == 0 else "failed"
    lines.append(f"Command:   {spec.command_id}")
    lines.append(f"Name:      {spec.title or spec.command_id}")
    lines.append(f"Status:    {status}")
    lines.append(f"Exit code: {run.exit_code}")
    lines.append(
        f"Started:   {run.start_ts.astimezone().strftime('%d-%b-%Y %H:%M:%S')}"
    )
    lines.append(f"Finished:  {run.end_ts.astimezone().strftime('%d-%b-%Y %H:%M:%S')}")
    lines.append(f"Duration:  {run.duration_seconds:.3f}s")

    if run.metadata and isinstance(run.metadata.get("delegation"), dict):
        deleg = run.metadata["delegation"]
        if deleg.get("dispatched"):
            info = deleg.get("delegate_info") or {}
            lines.append(
                f"Delegation: dispatched {info.get('action', '?')} "
                f"{info.get('id', '?')} ({info.get('title', '')})"
            )
        elif deleg.get("note"):
            lines.append(f"Delegation: {deleg['note']}")
        rejected = deleg.get("rejected")
        if rejected:
            lines.append(f"Rejected:  {len(rejected)} candidate(s)")

    if run.metadata and isinstance(run.metadata.get("pr_monitor"), dict):
        pm = run.metadata["pr_monitor"]
        ready_count = len(pm.get("ready_prs", []) or [])
        failing_count = len(pm.get("failing_prs", []) or [])
        skipped_count = len(pm.get("skipped_prs", []) or [])
        lines.append(f"Open PRs:  {int(pm.get('open_prs', pm.get('prs_checked', 0)) or 0)}")
        lines.append(f"Ready:     {ready_count}")
        lines.append(f"Failing:   {failing_count}")
        lines.append(f"Skipped:   {skipped_count}")
        lines.append(
            f"LLM Reviews: dispatched={int(pm.get('llm_reviews_dispatched', 0) or 0)}, "
            f"presented={int(pm.get('llm_reviews_presented', 0) or 0)}"
        )
        lines.append(
            f"Notify:    sent={int(pm.get('notifications_sent', 0) or 0)}"
        )
        lines.append(
            f"AutoReview:{str(bool(pm.get('auto_review_enabled', False))).lower()}"
        )

    if fmt == "full":
        lines.append(f"Instance:  {instance}")
        lines.append(f"Type:      {spec.command_type}")
        lines.append(f"Command:   {spec.command}")
        if output:
            lines.append("")
            lines.append("--- output ---")
            lines.append(output)
            lines.append("--- end output ---")
        elif output is not None:
            lines.append("")
            lines.append("(no output)")

    return "\n".join(lines)


def _format_command_detail(
    spec: CommandSpec, state: Dict[str, Any], fmt: str
) -> Dict[str, Any]:
    last_run = _from_iso(state.get("last_run_ts"))
    last_exit = state.get("last_exit_code")
    running = state.get("running", False)
    next_run: Optional[dt.datetime] = None
    if last_run is not None and spec.frequency_minutes > 0:
        next_run = last_run + dt.timedelta(minutes=spec.frequency_minutes)
    return {
        "id": spec.command_id,
        "name": spec.title or spec.command_id,
        "description": _command_description(spec),
        "type": spec.command_type,
        "frequency_minutes": spec.frequency_minutes,
        "priority": spec.priority,
        "requires_llm": spec.requires_llm,
        "running": running,
        "last_run": _to_iso(last_run),
        "last_exit_code": last_exit,
        "next_run": _to_iso(next_run),
    }


def _format_command_details_table(details: List[Dict[str, Any]], fmt: str) -> str:
    if not details:
        return "No commands configured."

    if fmt == "concise":
        lines: List[str] = []
        for d in details:
            status = "running" if d.get("running") else "idle"
            lines.append(f"{d['id']}  {d['name']}  {status}")
        return "\n".join(lines)

    if fmt == "raw":
        return _format_command_table(
            [
                {
                    "id": d["id"],
                    "name": d["name"],
                    "description": d.get("description", ""),
                    "last_run": d.get("last_run"),
                    "next_run": d.get("next_run"),
                }
                for d in details
            ]
        )

    blocks: List[str] = []
    for d in details:
        lines = []
        lines.append(f"  ID:          {d['id']}")
        lines.append(f"  Name:        {d['name']}")
        desc = d.get("description") or ""
        if desc:
            lines.append(f"  Description: {_truncate_text(desc, 80)}")
        last_run = d.get("last_run") or "never"
        if last_run not in ("never", None):
            parsed = _from_iso(str(last_run))
            if parsed is not None:
                last_run = parsed.astimezone().strftime("%d-%b-%Y %H:%M:%S")
        lines.append(f"  Last run:    {last_run}")
        next_run = d.get("next_run") or "n/a"
        if next_run not in ("n/a", None):
            parsed = _from_iso(str(next_run))
            if parsed is not None:
                next_run = parsed.astimezone().strftime("%d-%b-%Y %H:%M:%S")
        lines.append(f"  Next run:    {next_run}")
        if fmt == "full":
            lines.append(f"  Type:        {d.get('type', 'shell')}")
            lines.append(f"  Frequency:   {d.get('frequency_minutes', 0)}m")
            lines.append(f"  Priority:    {d.get('priority', 0)}")
            lines.append(f"  Requires LLM: {d.get('requires_llm', False)}")
            lines.append(f"  Running:     {d.get('running', False)}")
            last_exit = d.get("last_exit_code")
            if last_exit is not None:
                lines.append(f"  Last exit:   {last_exit}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _get_instance_name() -> str:
    try:
        return os.uname().nodename
    except Exception:
        return "(unknown)"


def _cli_run(args: argparse.Namespace) -> int:
    daemon.load_env()
    verbose = getattr(args, "verbose", False)
    if verbose:
        logging.getLogger("ampa").setLevel(logging.DEBUG)

    use_json = getattr(args, "json", False)
    fmt = getattr(args, "format", "normal") or "normal"
    watch_interval = getattr(args, "watch", None)
    command_id = getattr(args, "command_id", None)
    instance = _get_instance_name()

    if not command_id:
        store = _store_from_env()
        details = []
        for spec in store.list_commands():
            state = store.get_state(spec.command_id)
            details.append(_format_command_detail(spec, state, fmt))
        details.sort(key=lambda d: d.get("id") or "")
        if use_json:
            print(json.dumps(details, indent=2, sort_keys=True))
        else:
            print(_format_command_details_table(details, fmt))
        return 0

    # When watch mode is NOT active, try to delegate to the running daemon so
    # that the run appears in its logs and scheduler state.  Watch mode always
    # runs locally to allow real-time looping without HTTP round-trips.
    if watch_interval is None:
        daemon_result = _try_daemon_run(command_id)
        if daemon_result is not None:
            # Daemon executed the command; display the result locally.
            if "error" in daemon_result:
                if use_json:
                    print(json.dumps(daemon_result, indent=2))
                else:
                    print(daemon_result["error"])
                return 2
            if use_json:
                print(json.dumps(daemon_result, indent=2, sort_keys=True))
            else:
                # Reconstruct displayable objects from the daemon response.
                start_ts = _from_iso(daemon_result.get("started_at"))
                end_ts = _from_iso(daemon_result.get("finished_at"))
                if start_ts is None:
                    start_ts = _utc_now()
                if end_ts is None:
                    end_ts = start_ts
                run = CommandRunResult(
                    start_ts=start_ts,
                    end_ts=end_ts,
                    exit_code=int(daemon_result.get("exit_code", 0)),
                    output=daemon_result.get("output") or "",
                    metadata=daemon_result.get("metadata"),
                )
                spec = CommandSpec(
                    command_id=daemon_result.get("id", command_id),
                    command="",
                    requires_llm=False,
                    frequency_minutes=0,
                    priority=0,
                    metadata={},
                    title=daemon_result.get("name", command_id),
                    command_type="shell",
                )
                daemon_instance = daemon_result.get("instance") or instance
                print(_format_run_result_human(spec, run, fmt, daemon_instance))
            return int(daemon_result.get("exit_code", 0))

    scheduler = load_scheduler(command_cwd=os.getcwd())
    spec = scheduler.store.get_command(command_id)
    if spec is None:
        if use_json:
            print(json.dumps({"error": f"Unknown command id: {command_id}"}, indent=2))
        else:
            print(f"Unknown command id: {command_id}")
        return 2

    def _execute_once() -> int:
        if watch_interval is not None:
            ts = _utc_now().astimezone().strftime("%d-%b-%Y %H:%M:%S")
            if not use_json:
                print(f"[{ts}]")
        try:
            run = scheduler.start_command(spec)
        except Exception as exc:
            LOG.exception("Run failed for %s", command_id)
            report = build_error_report(
                exc,
                command="run",
                args={"command_id": command_id, "instance": instance},
            )
            if use_json:
                render_error_report_json(report, file=sys.stderr)
            else:
                render_error_report(report, file=sys.stderr, verbose=True)
            return report.exit_code
        if use_json:
            print(_format_run_result_json(spec, run, instance))
        else:
            print(_format_run_result_human(spec, run, fmt, instance))
        return int(run.exit_code)

    if watch_interval is not None:
        interval = max(1, int(watch_interval))
        last_exit = 0
        try:
            while True:
                last_exit = _execute_once()
                if not use_json and fmt != "concise":
                    print()
                time.sleep(interval)
        except KeyboardInterrupt:
            return last_exit
    else:
        return _execute_once()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AMPA scheduler")
    sub = parser.add_subparsers(dest="command")

    list_cmd = sub.add_parser("list", help="List scheduled commands")
    list_cmd.add_argument("--json", action="store_true", help="Output JSON")
    ls_cmd = sub.add_parser("ls", help="Alias for list")
    ls_cmd.add_argument("--json", action="store_true", help="Output JSON")

    add = sub.add_parser("add", help="Add a scheduled command")
    add.add_argument("command_id")
    add.add_argument("command")
    add.add_argument("frequency_minutes", type=int)
    add.add_argument("priority", type=int)
    add.add_argument("--requires-llm", action="store_true")
    add.add_argument("--metadata")
    add.add_argument("--max-runtime-minutes", type=int, dest="max_runtime_minutes")
    add.add_argument("--type", dest="command_type", default="shell")
    add.add_argument("--title")

    update = sub.add_parser("update", help="Update a scheduled command")
    update.add_argument("command_id")
    update.add_argument("command")
    update.add_argument("frequency_minutes", type=int)
    update.add_argument("priority", type=int)
    update.add_argument("--requires-llm", action="store_true")
    update.add_argument("--metadata")
    update.add_argument("--max-runtime-minutes", type=int, dest="max_runtime_minutes")
    update.add_argument("--type", dest="command_type", default="shell")
    update.add_argument("--title")

    remove = sub.add_parser("remove", help="Remove a scheduled command")
    remove.add_argument("command_id")

    dry_run = sub.add_parser("delegation", help="Generate a delegation report")
    dry_run.add_argument("--discord", action="store_true")

    run_once = sub.add_parser(
        "run-once", help="Run a command immediately by id (legacy alias for 'run')"
    )
    run_once.add_argument("command_id", nargs="?", default=None)
    run_once.add_argument("--json", action="store_true", help="Output in JSON format")
    run_once.add_argument(
        "--verbose",
        action="store_true",
        help="Show verbose output including debug messages",
    )
    run_once.add_argument(
        "-F",
        "--format",
        choices=["concise", "normal", "full", "raw"],
        default="normal",
        help="Human display format",
    )
    run_once.add_argument(
        "-w",
        "--watch",
        type=int,
        nargs="?",
        const=5,
        default=None,
        help="Rerun the command every N seconds (default: 5)",
    )

    run_cmd = sub.add_parser(
        "run",
        help=(
            "Run a scheduler command immediately by id, or list available commands. "
            "When a running daemon is detected (via AMPA_METRICS_PORT, default 8000) "
            "the command is forwarded to it so the run appears in the daemon log and "
            "scheduler store. Falls back to local execution if no daemon is available."
        ),
    )
    run_cmd.add_argument(
        "command_id",
        nargs="?",
        default=None,
        help="Command id to run; omit to list available commands",
    )
    run_cmd.add_argument("--json", action="store_true", help="Output in JSON format")
    run_cmd.add_argument(
        "--verbose",
        action="store_true",
        help="Show verbose output including debug messages",
    )
    run_cmd.add_argument(
        "-F",
        "--format",
        choices=["concise", "normal", "full", "raw"],
        default="normal",
        help="Human display format",
    )
    run_cmd.add_argument(
        "-w",
        "--watch",
        type=int,
        nargs="?",
        const=5,
        default=None,
        help=(
            "Rerun the command locally every N seconds (default: 5). "
            "Watch mode always runs locally rather than through the daemon."
        ),
    )

    return parser


def _cli_config(args: argparse.Namespace) -> int:
    """Persist operator configuration into the scheduler store.

    Usage: `wl ampa config [--auto-assign-enabled yes|no]`

    If `--auto-assign-enabled` is provided, write the value into the
    delegation command's metadata as `auto_assign_enabled`. If the
    delegation command is missing, register a minimal delegation command
    and set the metadata.
    If no flag is provided, print the current effective value.
    """
    store = _store_from_env()
    spec = store.get_command("delegation")
    if spec is None:
        # Create a minimal delegation command spec so the operator toggle
        # has a canonical home in the scheduler store.
        spec = CommandSpec(
            command_id="delegation",
            command="",
            requires_llm=False,
            frequency_minutes=1,
            priority=0,
            metadata={},
            title="Delegation Report",
            command_type="delegation",
        )
        store.add_command(spec)

    meta = spec.metadata if isinstance(spec.metadata, dict) else {}
    val = getattr(args, "auto_assign_enabled", None)
    if val is None:
        # Show current value
        current = meta.get("auto_assign_enabled")
        if current is None:
            # Fall back to legacy audit_only for operators who haven't set the new flag
            current = meta.get("audit_only")
        print(f"auto_assign_enabled: {current}")
        return 0

    # Normalize input
    normalized = str(val).strip().lower() in ("1", "true", "yes", "y", "on")
    meta["auto_assign_enabled"] = normalized
    # Persist change
    spec = CommandSpec(
        command_id=spec.command_id,
        command=spec.command,
        requires_llm=spec.requires_llm,
        frequency_minutes=spec.frequency_minutes,
        priority=spec.priority,
        metadata=meta,
        title=spec.title,
        max_runtime_minutes=spec.max_runtime_minutes,
        command_type=spec.command_type,
    )
    store.update_command(spec)
    print(f"Set auto_assign_enabled={normalized} on command 'delegation'")
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    start_cwd = os.getcwd()
    parser = _build_parser()
    args = parser.parse_args()
    if not args.command:
        scheduler = load_scheduler(command_cwd=start_cwd)
        scheduler.run_forever()
        return
    handlers = {
        "list": _cli_list,
        "ls": _cli_list,
        "add": _cli_add,
        "update": _cli_update,
        "remove": _cli_remove,
        "delegation": _cli_dry_run,
        "config": _cli_config,
        "run-once": lambda a: _cli_run(a),
        "run": _cli_run,
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise SystemExit(2)
    try:
        exit_code = handler(args)
        if exit_code:
            raise SystemExit(exit_code)
    except SystemExit:
        raise
    except Exception as exc:
        LOG.exception("Unhandled error in command '%s'", args.command)
        report = build_error_report(exc, command=args.command, args=vars(args))
        use_json = getattr(args, "json", False)
        if use_json:
            render_error_report_json(report, file=sys.stderr)
        else:
            render_error_report(report, file=sys.stderr, verbose=True)
        raise SystemExit(report.exit_code)


if __name__ == "__main__":
    main()
