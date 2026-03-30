import json

from plan.wl_adapter import WLAdapter


class DummyWL(WLAdapter):
    def __init__(self, responses=None):
        # responses is a dict mapping tuple(args) -> stdout
        self.responses = responses or {}

    def _run(self, args):
        key = tuple(args)
        if key in self.responses:
            return self.responses[key]
        # simulate CLI success with an empty JSON payload
        return json.dumps([])


def test_dep_add_and_idempotent():
    # simulate wl dep add succeeds (returns non-empty output) and is
    # idempotent when re-run (same success output)
    responses = {
        ("dep", "add", "SA-A", "SA-B"): "added",
    }
    w = DummyWL(responses)
    assert w.dep_add("SA-A", "SA-B") is True
    # re-run should also be considered success (idempotent at CLI-level)
    assert w.dep_add("SA-A", "SA-B") is True


def test_dep_add_cli_missing_returns_false():
    class FailWL(WLAdapter):
        def _run(self, args):
            return None

    w = FailWL()
    assert w.dep_add("SA-X", "SA-Y") is False


def test_dep_rm_and_idempotent():
    # simulate wl dep rm succeeds and re-running is a no-op success
    responses = {
        ("dep", "rm", "SA-A", "SA-B"): "removed",
    }
    w = DummyWL(responses)
    assert w.dep_rm("SA-A", "SA-B") is True
    assert w.dep_rm("SA-A", "SA-B") is True


def test_dep_list_parsing_and_failure():
    work_id = "SA-1"
    deps = [
        {"from": "SA-1", "to": "SA-2"},
        {"from": "SA-3", "to": "SA-1"},
    ]
    responses = {("dep", "list", work_id, "--json"): json.dumps(deps)}
    w = DummyWL(responses)
    out = w.dep_list(work_id)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["from"] == "SA-1"

    # simulate CLI failure (wl missing) -> adapter should return empty list
    class FailWL2(WLAdapter):
        def _run(self, args):
            return None

    w2 = FailWL2()
    assert w2.dep_list(work_id) == []
