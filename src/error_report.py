"""Centralised error-report helper for AMPA CLI commands.

This module provides :func:`build_error_report` and :func:`render_error_report`
which together produce a consistent, informative error report whenever an AMPA
CLI command encounters an unhandled / internal error.

Typical usage inside a CLI handler::

    from ampa.error_report import build_error_report, render_error_report

    try:
        do_work()
    except Exception as exc:
        report = build_error_report(exc, command="run", args={"id": "foo"})
        render_error_report(report, file=sys.stderr)
        return report.exit_code
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import platform
import sys
import traceback
from typing import Any, Dict, IO, Optional


@dataclasses.dataclass(frozen=True)
class ErrorReport:
    """Immutable value object that holds every piece of an error report."""

    command: str
    error_type: str
    error_message: str
    traceback: Optional[str]
    timestamp: str
    hostname: str
    python_version: str
    platform: str
    args: Dict[str, Any]
    exit_code: int

    # -- serialisation helpers -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return the report as a plain dict (suitable for JSON)."""
        return dataclasses.asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Return the report as a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, default=str)


def build_error_report(
    exc: BaseException,
    *,
    command: str = "",
    args: Optional[Dict[str, Any]] = None,
    exit_code: int = 1,
) -> ErrorReport:
    """Build an :class:`ErrorReport` from an exception and context metadata.

    Parameters
    ----------
    exc:
        The exception that triggered the report.
    command:
        Name of the CLI command that was running (e.g. ``"run"``).
    args:
        Arbitrary context about the invocation (CLI flags, IDs, etc.).
    exit_code:
        Suggested process exit code (defaults to ``1``).

    Returns
    -------
    ErrorReport
        A frozen dataclass ready for rendering or serialisation.
    """
    tb: Optional[str] = None
    if exc.__traceback__ is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    try:
        hostname = os.uname().nodename
    except Exception:  # pragma: no cover
        hostname = "(unknown)"

    return ErrorReport(
        command=command,
        error_type=type(exc).__qualname__,
        error_message=str(exc),
        traceback=tb,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        hostname=hostname,
        python_version=platform.python_version(),
        platform=platform.platform(),
        args=args or {},
        exit_code=exit_code,
    )


def render_error_report(
    report: ErrorReport,
    *,
    file: IO[str] | None = None,
    verbose: bool = False,
) -> str:
    """Render a human-readable error report and optionally print it.

    Parameters
    ----------
    report:
        The :class:`ErrorReport` to render.
    file:
        If provided, the rendered text is written to this stream
        (e.g. ``sys.stderr``).
    verbose:
        When *True*, the full traceback is included.  When *False*
        (the default), the traceback is omitted to keep output concise
        for end users.

    Returns
    -------
    str
        The rendered report text (always returned regardless of *file*).
    """
    lines = [
        "=== AMPA Error Report ===",
        f"Command:   {report.command or '(unknown)'}",
        f"Error:     {report.error_type}: {report.error_message}",
        f"Timestamp: {report.timestamp}",
        f"Host:      {report.hostname}",
        f"Python:    {report.python_version}",
        f"Platform:  {report.platform}",
    ]

    if report.args:
        try:
            args_str = json.dumps(report.args, sort_keys=True, default=str)
        except Exception:  # pragma: no cover
            args_str = str(report.args)
        lines.append(f"Args:      {args_str}")

    if verbose and report.traceback:
        lines.append("")
        lines.append("--- traceback ---")
        lines.append(report.traceback.rstrip())
        lines.append("--- end traceback ---")

    lines.append(f"Exit code: {report.exit_code}")
    lines.append("=========================")

    text = "\n".join(lines)

    if file is not None:
        print(text, file=file)

    return text


def render_error_report_json(
    report: ErrorReport,
    *,
    file: IO[str] | None = None,
) -> str:
    """Render the report as JSON and optionally print it.

    Parameters
    ----------
    report:
        The :class:`ErrorReport` to render.
    file:
        If provided, the JSON text is written to this stream.

    Returns
    -------
    str
        The JSON string.
    """
    text = report.to_json()
    if file is not None:
        print(text, file=file)
    return text
