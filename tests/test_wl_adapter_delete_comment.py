import json
import subprocess
from plan.wl_adapter import WLAdapter


class DummyWL(WLAdapter):
    def __init__(self, responses):
        # responses is a dict mapping tuple(args) -> stdout
        self.responses = responses

    def _run(self, args):
        key = tuple(args)
        if key in self.responses:
            return self.responses[key]
        # simulate CLI success with empty output
        return json.dumps({})


def test_delete_comment_success_and_verify_absent():
    work_id = "SA-TEST-1"
    comment_id = "C1"
    # simulate delete returns some success output, and show later omits comment
    responses = {
        ("comment", "delete", f"{work_id}-{comment_id}"): json.dumps({"success": True}),
        ("show", work_id, "--json"): json.dumps(
            {"workItem": {"id": work_id, "comments": []}}
        ),
    }
    w = DummyWL(responses)
    assert w.delete_comment(work_id, comment_id) is True


def test_delete_comment_missing_still_present():
    work_id = "SA-TEST-2"
    comment_id = "C2"
    # delete reports success but show still shows the comment
    responses = {
        ("comment", "delete", f"{work_id}-{comment_id}"): json.dumps({"success": True}),
        ("show", work_id, "--json"): json.dumps(
            {
                "workItem": {
                    "id": work_id,
                    "comments": [{"id": f"{work_id}-{comment_id}", "body": "hi"}],
                }
            }
        ),
    }
    w = DummyWL(responses)
    # sanity-check the show output before deletion
    shown = w.show(work_id)
    print("DEBUG show output:", shown)
    assert shown is not None
    assert isinstance(shown, dict)
    assert shown.get("workItem") and isinstance(
        shown.get("workItem").get("comments"), list
    )
    # deletion should be considered failed because comment remains
    assert w.delete_comment(work_id, comment_id) is False


def test_delete_comment_cli_failure():
    work_id = "SA-TEST-3"
    comment_id = "C3"

    # simulate delete CLI missing (None) -> failure
    class FailWL(WLAdapter):
        def _run(self, args):
            return None

    w = FailWL()
    assert w.delete_comment(work_id, comment_id) is False
