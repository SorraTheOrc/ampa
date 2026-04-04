#!/usr/bin/env python3
"""Wrapper script that delegates to the OpenCode 'triage' skill implementation.

Tests expect to be able to execute skill/triage/scripts/check_or_create.py from the
repository root. The canonical implementation lives in the user's OpenCode config
directory (~/.config/opencode/skill/triage/scripts/check_or_create.py). Import
the package `skill.triage.scripts.check_or_create` (which is configured to
prefer the external skill directory) and invoke its main() function. If that
fails, fall back to executing the external script by path.

This wrapper keeps the tests fast and avoids duplicating implementation.
"""
from __future__ import annotations

import importlib
import os
import runpy
import sys


def _run_via_import() -> int:
    try:
        mod = importlib.import_module("skill.triage.scripts.check_or_create")
    except Exception:
        return 2

    # Prefer calling the module's main if present so it controls stdout/exit.
    if hasattr(mod, "main"):
        try:
            mod.main()
            return 0
        except SystemExit as se:
            # Propagate the exit code used by the module
            code = se.code if isinstance(se.code, int) else 1
            return code
        except Exception:
            return 2

    # If the module does not expose a main, fall back to executing the file
    return 2


def _run_via_path() -> int:
    external = os.path.expanduser("~/.config/opencode/skill/triage/scripts/check_or_create.py")
    if os.path.exists(external):
        try:
            runpy.run_path(external, run_name="__main__")
            return 0
        except SystemExit as se:
            return se.code if isinstance(se.code, int) else 1
        except Exception:
            return 2
    return 2


def main() -> int:
    code = _run_via_import()
    if code == 0:
        return 0
    # Try running the external script file directly as a fallback
    code = _run_via_path()
    if code == 0:
        return 0
    print("Could not locate or run triage script (skill.triage.scripts.check_or_create)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
