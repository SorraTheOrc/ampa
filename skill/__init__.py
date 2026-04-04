"""Compatibility shim to expose local OpenCode 'skill' plugins to tests.

Pytests in this environment expect a top-level `skill` package. The
actual skill implementations live in the user's OpenCode config directory
(`~/.config/opencode/skill`). Add that directory to the package search
path at import time so tests can import skill.<name> as if it were a
normal installed package.

This file is intentionally small and only executed on import.
"""
from __future__ import annotations

import os
from pathlib import Path

# Keep the repo-local package path first, but allow the global OpenCode
# skill directory to provide subpackages (owner_inference, triage, etc.).
__path__ = [str(Path(__file__).parent.resolve())]

_external = Path.home() / ".config" / "opencode" / "skill"
if _external.exists() and _external.is_dir():
    # Prefer external skills so tests that expect the shared implementations
    # behave the same as the developer environment.
    __path__.insert(0, str(_external.resolve()))
