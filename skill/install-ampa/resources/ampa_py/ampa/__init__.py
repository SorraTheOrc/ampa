"""Minimal ampa package stub used by the installer resources.

This package mirrors the runtime layout expected by the launcher so the
installer can copy a working ampa package into target projects that don't
already include it.
"""

__all__ = ["conversation_manager", "daemon", "scheduler", "responder"]
