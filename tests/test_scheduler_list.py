import datetime as dt

from ampa.scheduler_types import CommandSpec
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_cli import _build_command_listing


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def test_build_command_listing_empty():
    store = DummyStore()
    assert _build_command_listing(store) == []


def test_build_command_listing_formats_runs():
    store = DummyStore()
    spec = CommandSpec("cmd", "echo hi", False, 10, 0, {}, title="Example")
    store.add_command(spec)
    last_run = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    store.update_state("cmd", {"last_run_ts": last_run.isoformat()})

    rows = _build_command_listing(store, now=last_run)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "cmd"
    assert row["name"] == "Example"
    assert row["last_run"] == last_run.isoformat()
    assert row["next_run"] == (last_run + dt.timedelta(minutes=10)).isoformat()


def test_format_command_table_uses_local_time():
    store = DummyStore()
    last_run = dt.datetime(2026, 1, 2, 15, 4, tzinfo=dt.timezone.utc)
    spec = CommandSpec("cmd", "echo hi", False, 10, 0, {}, title="Example")
    store.add_command(spec)
    store.update_state("cmd", {"last_run_ts": last_run.isoformat()})

    rows = _build_command_listing(store, now=last_run)
    from ampa.scheduler_cli import _format_command_table

    table = _format_command_table(rows)
    local_last = last_run.astimezone().strftime("%d-%b-%Y %H:%M")
    assert local_last in table


def test_build_command_listing_never_run():
    store = DummyStore()
    spec = CommandSpec("cmd", "echo hi", False, 10, 0, {}, title=None)
    store.add_command(spec)

    rows = _build_command_listing(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "cmd"
    assert row["name"] == "cmd"
    assert row["last_run"] is None
    assert row["next_run"] is None
