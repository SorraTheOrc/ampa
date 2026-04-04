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


def test_select_candidate_tie_breaks_stably_preserve_input_order():
    # When two items have identical timestamps, the selection should be
    # deterministic and preserve the input order (Python's sort is stable).
    items = [
        _make_item("A", "2023-01-01T00:00:00Z"),
        _make_item("B", "2023-01-01T00:00:00Z"),
    ]

    # Repeated calls should always select the first occurrence when timestamps
    # are equal, demonstrating stable tie-breaking.
    first = audit_poller._select_candidate(items)
    second = audit_poller._select_candidate(list(items))
    assert first is not None and second is not None
    assert first["id"] == "A"
    assert second["id"] == "A"


def test_select_candidate_performance_microbenchmark():
    # A micro-benchmark to ensure the selection scales linearly and avoids
    # reparsing per comparison. This doesn't assert a strict time budget but
    # ensures the implementation completes quickly for a large number of
    # candidates during unit tests.
    import time

    n = 1000
    items = [_make_item(str(i), "2023-01-01T00:00:00Z") for i in range(n)]
    start = time.perf_counter()
    selected = audit_poller._select_candidate(items)
    elapsed = time.perf_counter() - start
    assert selected is not None
    # Sanity: should complete in a short time under reasonable CI machines.
    # 0.5s is generous for pure-Python quick micro-benchmark here.
    assert elapsed < 0.5, f"_select_candidate too slow: {elapsed:.3f}s"
