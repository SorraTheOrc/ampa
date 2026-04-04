import datetime as dt

from ampa import audit_poller


def _make_item(wid: str):
    return {"id": wid, "title": f"Item {wid}", "updatedAt": "2020-01-01T00:00:00Z"}


def test_filter_excludes_within_cooldown():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    item = _make_item("I-cooldown")
    candidates = [item]

    # last audit was 1 hour ago, cooldown 2 hours -> should be excluded
    last_audit_by_item = {"I-cooldown": (now - dt.timedelta(hours=1)).isoformat()}
    out = audit_poller._filter_by_cooldown(candidates, last_audit_by_item, cooldown_hours=2, now=now)
    assert out == []


def test_filter_includes_at_boundary():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    item = _make_item("I-boundary")
    candidates = [item]

    # last audit was exactly 2 hours ago, cooldown 2 hours -> should be included
    last_audit_by_item = {"I-boundary": (now - dt.timedelta(hours=2)).isoformat()}
    out = audit_poller._filter_by_cooldown(candidates, last_audit_by_item, cooldown_hours=2, now=now)
    assert out == candidates


def test_filter_includes_missing_store_entry():
    now = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    item = _make_item("I-nostore")
    candidates = [item]

    # no entry in last_audit_by_item -> should be included regardless of cooldown
    last_audit_by_item = {}
    out = audit_poller._filter_by_cooldown(candidates, last_audit_by_item, cooldown_hours=6, now=now)
    assert out == candidates
