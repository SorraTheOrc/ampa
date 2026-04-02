import datetime as dt

from ampa import scheduler_types


def test_from_iso_z_terminator():
    t = scheduler_types._from_iso("2023-01-02T03:04:05Z")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_plus_offset():
    t = scheduler_types._from_iso("2023-01-02T03:04:05+00:00")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_naive_assumed_utc():
    t = scheduler_types._from_iso("2023-01-02T03:04:05")
    assert t is not None
    # Naive timestamps should be coerced to UTC
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)
