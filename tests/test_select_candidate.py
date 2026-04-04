import datetime as dt
from typing import Optional

from ampa import audit_poller


def _make_item(wid: str, updated: Optional[str]):
    d = {"id": wid}
    if updated is not None:
        d["updatedAt"] = updated
    return d


def test_select_candidate_prefers_missing_updated_at_first():
    # Items with no updatedAt should sort before those with timestamps
    items = [
        _make_item("A", "2023-01-02T00:00:00Z"),
        _make_item("B", None),
        _make_item("C", "2022-12-31T23:59:59Z"),
    ]

    selected = audit_poller._select_candidate(items)
    assert selected is not None
    assert selected["id"] == "B"


def test_select_candidate_selects_oldest_timestamp():
    # When all items have timestamps, choose the oldest (earliest) one.
    items = [
        _make_item("A", "2023-01-02T00:00:00Z"),
        _make_item("B", "2023-01-01T00:00:00Z"),
        _make_item("C", "2023-01-03T00:00:00Z"),
    ]

    selected = audit_poller._select_candidate(items)
    assert selected is not None
    assert selected["id"] == "B"
