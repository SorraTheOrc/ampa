"""Shared data classes and utility functions for the AMPA scheduler.

This module contains the pure data types (``CommandSpec``, ``SchedulerConfig``,
``RunResult``, ``CommandRunResult``) and stateless utility functions
(``_utc_now``, ``_to_iso``, ``_from_iso``, ``_seconds_between``, ``_bool_meta``)
 that are used across the scheduler, delegation, audit, CLI, and store
modules.

Extracting these into their own module breaks the dependency on
``ampa.scheduler`` for modules that only need data classes or time helpers,
eliminating circular-import risks.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
from typing import Any, Dict, Optional

LOG = logging.getLogger("ampa.scheduler")


# ---------------------------------------------------------------------------
# Time / conversion utilities
# ---------------------------------------------------------------------------


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        # Accept common ISO forms including trailing 'Z' (UTC) by normalizing
        # to an offset-aware representation that datetime.fromisoformat can parse.
        v = value
        if isinstance(v, str) and v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return dt.datetime.fromisoformat(v)
    except Exception:
        return None


def _seconds_between(now: dt.datetime, then: Optional[dt.datetime]) -> Optional[float]:
    if then is None:
        return None
    return (now - then).total_seconds()


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _bool_meta(value: Any) -> bool:
    """Coerce a metadata value to bool.

    Accepts ``bool``, ``None``, and common truthy string representations
    (``"1"``, ``"true"``, ``"yes"``, ``"y"``, ``"on"``).
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CommandSpec:
    # Keep positional ordering compatible with existing tests and callers.
    command_id: str
    command: str
    requires_llm: bool
    frequency_minutes: int
    priority: int
    metadata: Dict[str, Any]
    title: Optional[str] = None
    max_runtime_minutes: Optional[int] = None
    command_type: str = "shell"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.command_id,
            "command": self.command,
            "title": self.title,
            "requires_llm": self.requires_llm,
            "frequency_minutes": self.frequency_minutes,
            "priority": self.priority,
            "metadata": self.metadata,
            "max_runtime_minutes": self.max_runtime_minutes,
            "type": self.command_type,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "CommandSpec":
        return CommandSpec(
            command_id=str(data["id"]),
            command=str(data.get("command", "")),
            requires_llm=bool(data.get("requires_llm", False)),
            frequency_minutes=int(data.get("frequency_minutes", 1)),
            priority=int(data.get("priority", 0)),
            metadata=dict(data.get("metadata", {})),
            title=data.get("title"),
            max_runtime_minutes=data.get("max_runtime_minutes"),
            command_type=str(data.get("type", "shell")),
        )


@dataclasses.dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_seconds: int
    global_min_interval_seconds: int
    priority_weight: float
    store_path: str
    llm_healthcheck_url: str
    max_run_history: int
    # Make this optional with a sensible default to preserve backwards
    # compatibility for callers/tests that instantiate SchedulerConfig
    # without this value.
    container_dispatch_timeout_seconds: int = 240

    @staticmethod
    def from_env() -> "SchedulerConfig":
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name, str(default))
            try:
                value = int(raw)
                if value <= 0:
                    raise ValueError("must be positive")
                return value
            except Exception:
                LOG.warning("Invalid %s=%r; using %s", name, raw, default)
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name, str(default))
            try:
                value = float(raw)
                if value < 0:
                    raise ValueError("must be non-negative")
                return value
            except Exception:
                LOG.warning("Invalid %s=%r; using %s", name, raw, default)
                return default

        # The scheduler store MUST exist at the local per-project path.
        # The daemon is spawned with cwd=projectRoot (ampa.mjs) so
        # os.getcwd() gives the correct project root at startup.
        store_path = os.path.join(
            os.getcwd(), ".worklog", "ampa", "scheduler_store.json"
        )
        return SchedulerConfig(
            poll_interval_seconds=_int("AMPA_SCHEDULER_POLL_INTERVAL_SECONDS", 5),
            global_min_interval_seconds=_int(
                "AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS", 60
            ),
            priority_weight=_float("AMPA_SCHEDULER_PRIORITY_WEIGHT", 0.1),
            store_path=store_path,
            llm_healthcheck_url=os.getenv(
                "AMPA_LLM_HEALTHCHECK_URL", "http://localhost:8000/health"
            ),
            max_run_history=_int("AMPA_SCHEDULER_MAX_RUN_HISTORY", 50),
            container_dispatch_timeout_seconds=_int(
                "AMPA_CONTAINER_DISPATCH_TIMEOUT", 240
            ),
        )


@dataclasses.dataclass(frozen=True)
class RunResult:
    start_ts: dt.datetime
    end_ts: dt.datetime
    exit_code: int
    metadata: Optional[Dict[str, Any]] = dataclasses.field(default=None)

    @property
    def duration_seconds(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds()


@dataclasses.dataclass(frozen=True)
class CommandRunResult(RunResult):
    output: str = ""
