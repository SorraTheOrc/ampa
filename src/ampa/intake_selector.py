"""Query and select intake candidates from Worklog.

Provides a small, focused IntakeCandidateSelector that executes
`wl next --stage idea --json`, parses the JSON output, and returns the
single highest-priority candidate (by numeric `sortIndex`, descending).

The module mirrors patterns used by the audit poller: defensive JSON
parsing, tolerant of several response shapes, and deterministic tie
breaking using `updated_at` and id ordering.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .audit_poller import _from_iso

LOG = logging.getLogger("ampa.intake_selector")


@dataclass(frozen=True)
class IntakeCandidateSelector:
    """Selector for intake candidates.

    Methods are small and pure to make testing easy: `query_candidates`
    executes the shell command (via provided runner), `select_top`
    performs sorting and selection.
    """

    run_shell: Any
    cwd: str

    def query_candidates(self, timeout: int = 60) -> Optional[List[Dict[str, Any]]]:
        """Run `wl next --stage idea --json` and return a list of work item dicts.

        Returns None on query failure (non-zero exit or invalid JSON). An
        empty list means query succeeded but no "idea" items were found.
        """
        try:
            proc = self.run_shell(
                "wl next --stage idea --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=timeout,
            )
        except Exception:
            LOG.exception("wl next --stage idea command failed to execute")
            return None

        if proc.returncode != 0:
            LOG.warning("wl next --stage idea exited with code %s: %s", proc.returncode, proc.stderr)
            return None

        try:
            raw = json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl next --stage idea output as JSON")
            return None

        items: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            items.extend([it for it in raw if isinstance(it, dict)])
        elif isinstance(raw, dict):
            # common wrappers
            for key in ("workItems", "work_items", "items", "data"):
                val = raw.get(key)
                if isinstance(val, list):
                    items.extend([it for it in val if isinstance(it, dict)])
                    break
            if not items:
                for k, v in raw.items():
                    if isinstance(v, list) and k.lower().endswith("workitems"):
                        items.extend([it for it in v if isinstance(it, dict)])
                        break

        # Normalize id/key
        normalized: List[Dict[str, Any]] = []
        for it in items:
            wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
            if not wid:
                continue
            candidate = dict(it)
            candidate["id"] = str(wid)
            normalized.append(candidate)

        # Defensive filter: if the candidate includes an explicit stage/status
        # and it is not "idea", drop it. If no stage/status is present we
        # preserve the candidate to retain compatibility with older/wrapper
        # shapes that may omit explicit stage fields.
        filtered: List[Dict[str, Any]] = []
        for c in normalized:
            st = c.get("stage") or c.get("status")
            if st is None:
                filtered.append(c)
            else:
                try:
                    if str(st) == "idea":
                        filtered.append(c)
                    else:
                        LOG.info("Intake selector: dropping candidate %s with stage=%s", c.get("id"), st)
                except Exception:
                    # Be conservative: keep the candidate if any unexpected error
                    # occurs while evaluating the stage field.
                    filtered.append(c)

        return filtered

    def _item_sort_key(self, item: Dict[str, Any]):
        """Return a tuple sort key: (-sortIndex, -updated_ts, id) for descending priority.

        Uses numeric sortIndex (default 0). For updated timestamp we prefer
        newer items (later updated) to break ties (descending). The final
        tie-breaker is the item id (lexicographic) to provide deterministic
        ordering.
        """
        # sortIndex may be under different keys; be defensive
        si = item.get("sortIndex")
        try:
            si_val = float(si) if si is not None else 0.0
        except Exception:
            si_val = 0.0

        # updated timestamp parsing — reuse _from_iso from audit_poller
        updated = item.get("updatedAt") or item.get("updated_at") or item.get("updated")
        parsed = _from_iso(updated) if isinstance(updated, str) else None
        # For sorting, we want newer first -> use timestamp as float seconds
        ts = parsed.timestamp() if parsed is not None else 0.0

        # Negative values because Python sorts ascending by default
        return (-si_val, -ts, item.get("id", ""))

    def select_top(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Select the single top candidate by priority or return None."""
        if not candidates:
            return None
        try:
            sorted_candidates = sorted(candidates, key=self._item_sort_key)
        except Exception:
            LOG.exception("Failed to sort candidates; falling back to first item")
            return candidates[0]
        return sorted_candidates[0]
