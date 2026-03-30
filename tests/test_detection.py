import datetime
import pytest


def select_blocker(items):
    """Utility: items is list of dicts with keys: id, sortIndex (int), createdAt (ISO str)
    Selection: highest sortIndex wins; ties broken by earliest createdAt.
    Returns the selected blocker id.
    """

    def key(i):
        # sortIndex desc, createdAt asc
        return (-int(i.get("sortIndex", 0)), i.get("createdAt"))

    items_sorted = sorted(items, key=key)
    return items_sorted[0]["id"]


def test_select_blocker_sortindex_then_createdat():
    items = [
        {"id": "A", "sortIndex": 100, "createdAt": "2026-01-02T00:00:00Z"},
        {"id": "B", "sortIndex": 100, "createdAt": "2026-01-01T00:00:00Z"},
        {"id": "C", "sortIndex": 50, "createdAt": "2026-01-01T00:00:00Z"},
    ]
    assert select_blocker(items) == "B"


def test_select_blocker_sortindex_wins_over_createdat():
    items = [
        {"id": "A", "sortIndex": 200, "createdAt": "2026-01-05T00:00:00Z"},
        {"id": "B", "sortIndex": 100, "createdAt": "2026-01-01T00:00:00Z"},
    ]
    assert select_blocker(items) == "A"
