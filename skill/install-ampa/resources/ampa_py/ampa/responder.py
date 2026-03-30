"""Minimal responder shim for bundled installer package.

This module is intentionally small; real projects will provide a full
implementation. The stub is provided so imports succeed when the package is
installed by the installer into a project that lacks a local `ampa/`.
"""


def noop(*args, **kwargs):
    return None
