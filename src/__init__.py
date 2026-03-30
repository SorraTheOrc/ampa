"""AMPA package marker. Keeps the ampa directory importable as a package."""

from . import conversation_manager, responder

# Ensure package-level imports work when the package is imported from an
# installer layout where the parent directory is added to PYTHONPATH. Some
# modules historically used bare imports (e.g. `import conversation_manager`)
# which fail when executed within a package; prefer package-relative imports
# throughout the codebase and keep this module minimal.

__all__ = [
    "daemon",
    "scheduler",
    "selection",
    "conversation_manager",
    "responder",
]
