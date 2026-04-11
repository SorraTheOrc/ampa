import json
import types
import datetime as dt

from ampa.intake_selector import IntakeCandidateSelector


class DummyProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def make_runner(stdout: str, returncode: int = 0, stderr: str = ""):
    def run_shell(cmd, **kwargs):
        return DummyProc(stdout=stdout, returncode=returncode, stderr=stderr)

    return run_shell


def test_query_empty_list(tmp_path):
    runner = make_runner("[]")
    sel = IntakeCandidateSelector(run_shell=runner, cwd=str(tmp_path))
    items = sel.query_candidates()
    assert items == []


def test_query_wrapped_list(tmp_path):
    data = {"workItems": [{"id": "A", "sortIndex": 10}]}
    runner = make_runner(json.dumps(data))
    sel = IntakeCandidateSelector(run_shell=runner, cwd=str(tmp_path))
    items = sel.query_candidates()
    assert isinstance(items, list)
    assert len(items) == 1
    assert items[0]["id"] == "A"


def test_select_top_by_sortindex(tmp_path):
    # Two items; one with higher sortIndex should be selected
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    items = [
        {"id": "one", "sortIndex": 5, "updatedAt": now},
        {"id": "two", "sortIndex": 10, "updatedAt": now},
    ]
    sel = IntakeCandidateSelector(run_shell=lambda *a, **k: None, cwd=str(tmp_path))
    top = sel.select_top(items)
    assert top is not None
    assert top["id"] == "two"


def test_select_top_tiebreak_by_updated(tmp_path):
    # Same sortIndex — newer updatedAt should win
    older = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    newer = dt.datetime.now(dt.timezone.utc).isoformat()
    items = [
        {"id": "a", "sortIndex": 10, "updatedAt": older},
        {"id": "b", "sortIndex": 10, "updatedAt": newer},
    ]
    sel = IntakeCandidateSelector(run_shell=lambda *a, **k: None, cwd=str(tmp_path))
    top = sel.select_top(items)
    assert top["id"] == "b"


def test_query_invalid_json(tmp_path):
    runner = make_runner("not-a-json", returncode=0)
    sel = IntakeCandidateSelector(run_shell=runner, cwd=str(tmp_path))
    items = sel.query_candidates()
    assert items is None
