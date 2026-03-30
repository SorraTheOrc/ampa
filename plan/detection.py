from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, MutableMapping


def _parse_created_at(value: str) -> datetime:
    """Parse createdAt value to datetime. Fall back to epoch on error."""
    if not value:
        return datetime.fromtimestamp(0)
    try:
        # support ISO 8601-ish strings
        return datetime.fromisoformat(value)
    except Exception:
        try:
            # try common fallback: seconds since epoch
            return datetime.fromtimestamp(float(value))
        except Exception:
            return datetime.fromtimestamp(0)


def choose_blocker(items: Iterable[dict]) -> str | None:
    """Choose the blocker workitem id from a list of workitems.

    Selection rules (canonical):
    - Highest `sortIndex` wins (missing => 0)
    - If tie, earliest `createdAt` wins

    Returns the chosen workitem's `id`, or None if input is empty.
    """
    items = list(items)
    if not items:
        return None

    def key(it: dict):
        sort_index = it.get("sortIndex") or 0
        # we want highest sortIndex first, so use negative for descending
        created = _parse_created_at(it.get("createdAt"))
        return (-int(sort_index), created)

    chosen = min(items, key=key)
    return chosen.get("id")


def group_overlaps(children: Iterable[dict]) -> Dict[str, List[dict]]:
    """Group workitems by exact file path overlaps.

    Each child may provide an `allowed_files` list of exact paths (strings).
    The function returns a mapping path -> list of children that reference that path.

    Note: this uses exact path matching. Pattern/glob support can be added later.
    """
    groups: MutableMapping[str, List[dict]] = {}
    for child in children:
        for path in child.get("allowed_files", []) or []:
            groups.setdefault(path, []).append(child)
    return dict(groups)
