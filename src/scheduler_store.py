"""Persistent scheduler store — extracted from scheduler.py.

Canonical home for the ``SchedulerStore`` class.  Other modules should
import directly from here::

    from ampa.scheduler_store import SchedulerStore
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from .scheduler_types import CommandSpec, _utc_now, _to_iso, _from_iso

LOG = logging.getLogger("ampa.scheduler")


class SchedulerStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("store root must be object")
                data.setdefault("commands", {})
                data.setdefault("state", {})
                data.setdefault("last_global_start_ts", None)
                # append-only dispatch records for delegation actions
                data.setdefault("dispatches", [])
                return data
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Scheduler store not found at {self.path}. "
                "The local scheduler_store.json must exist at "
                "<projectRoot>/.worklog/ampa/scheduler_store.json before "
                "starting the scheduler. Copy scheduler_store_example.json "
                "to this location and configure your commands."
            ) from None
        except Exception:
            LOG.exception("Failed to read scheduler store at %s", self.path)
            raise

    def save(self) -> None:
        dir_name = os.path.dirname(self.path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)

    def append_dispatch(self, record: Dict[str, Any], retain_last: int = 100) -> str:
        """Append an append-only dispatch record and persist the store.

        Returns the generated dispatch id.
        """
        try:
            dispatch_id = record.get("id") or uuid.uuid4().hex
            record = dict(record)
            record["id"] = str(dispatch_id)
            record.setdefault("ts", _utc_now().isoformat())
            record.setdefault("session", uuid.uuid4().hex)
            # Best-effort runner identity
            try:
                record.setdefault("runner", getpass.getuser())
            except Exception:
                record.setdefault("runner", os.getenv("USER") or "(unknown)")
            self.data.setdefault("dispatches", []).append(record)
            # retention: keep only the most recent `retain_last` entries
            try:
                if isinstance(self.data.get("dispatches"), list):
                    self.data["dispatches"] = self.data["dispatches"][
                        -int(retain_last) :
                    ]
            except Exception:
                pass
            self.save()
            return str(dispatch_id)
        except Exception:
            LOG.exception("Failed to append dispatch record")
            # Fallback: return a best-effort id
            return str(uuid.uuid4().hex)

    def list_commands(self) -> List[CommandSpec]:
        return [
            CommandSpec.from_dict(value)
            for value in self.data.get("commands", {}).values()
        ]

    def add_command(self, spec: CommandSpec) -> None:
        self.data.setdefault("commands", {})[spec.command_id] = spec.to_dict()
        self.data.setdefault("state", {}).setdefault(spec.command_id, {})
        self.save()

    def remove_command(self, command_id: str) -> None:
        self.data.get("commands", {}).pop(command_id, None)
        self.data.get("state", {}).pop(command_id, None)
        self.save()

    def update_command(self, spec: CommandSpec) -> None:
        if spec.command_id not in self.data.get("commands", {}):
            raise KeyError(f"Unknown command id {spec.command_id}")
        self.data["commands"][spec.command_id] = spec.to_dict()
        self.save()

    def get_command(self, command_id: str) -> Optional[CommandSpec]:
        payload = self.data.get("commands", {}).get(command_id)
        if not payload:
            return None
        return CommandSpec.from_dict(payload)

    def get_state(self, command_id: str) -> Dict[str, Any]:
        return dict(self.data.get("state", {}).get(command_id, {}))

    def update_state(self, command_id: str, state: Dict[str, Any]) -> None:
        self.data.setdefault("state", {})[command_id] = state
        self.save()

    def update_global_start(self, when: dt.datetime) -> None:
        self.data["last_global_start_ts"] = _to_iso(when)
        self.save()

    def last_global_start(self) -> Optional[dt.datetime]:
        return _from_iso(self.data.get("last_global_start_ts"))

    def get_candidate_hash_cache(self) -> Dict[str, str]:
        """Return the persisted candidate hash cache as a ``{hash: iso_ts}`` dict."""
        raw = self.data.get("candidate_hash_cache", {})
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    def update_candidate_hash_cache(self, entries: Dict[str, str]) -> None:
        """Persist an updated ``{hash: iso_ts}`` candidate hash cache and save."""
        self.data["candidate_hash_cache"] = dict(entries)
        self.save()
