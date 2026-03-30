#!/usr/bin/env python3
"""Minimal scheduler_cli shim for packaged ampa used in tests.

This file provides a tiny, safe implementation that is importable as
`ampa.scheduler_cli` and can be executed as a CLI module. It intentionally
returns empty lists / success responses so tests that only rely on the
presence of the module or invocation succeed.
"""
from __future__ import annotations
import sys
import json

def list_schedules() -> None:
    """Print an empty list of schedules in JSON form."""
    print(json.dumps([]))

def run_schedule(name: str | None = None) -> None:
    """Print a minimal success object for running a schedule."""
    print(json.dumps({"status": "ok", "name": name}))

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write("usage: ampa.scheduler_cli [list|run <name>]\n")
        return 0
    cmd = argv[0]
    if cmd == "list":
        list_schedules()
        return 0
    if cmd == "run":
        run_schedule(argv[1] if len(argv) > 1 else None)
        return 0
    sys.stderr.write(f"unknown command: {cmd}\n")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
