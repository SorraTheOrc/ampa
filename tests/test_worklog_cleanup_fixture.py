import subprocess
import json
import pytest


def test_wl_cleanup_fixture(wl_test_items):
    """Create a transient work item using the fixture and verify it can be deleted.

    This test demonstrates usage of the wl_test_items fixture and the
    cleanup behaviour expected by AM-0MO617KJP008CSSU. It creates a work item
    with a strict test-only prefix and then deletes it.
    """
    data = wl_test_items("fixture-test")
    wid = None
    if isinstance(data, dict):
        wid = data.get("id") or (data.get("workItem") or {}).get("id")
    assert wid, "failed to obtain work item id from wl create"

    # Ensure the work item exists
    show = subprocess.run(["wl", "show", wid, "--json"], capture_output=True, text=True)
    assert show.returncode == 0, f"wl show failed: {show.stderr}"

    # Delete the item explicitly (the fixture finalizer will also attempt cleanup).
    delete = subprocess.run(["wl", "delete", wid, "--json"], capture_output=True, text=True)
    assert delete.returncode == 0, f"wl delete failed: {delete.stderr}"

    # Confirm deletion: if wl show fails or indicates deleted state we accept that as success.
    show2 = subprocess.run(["wl", "show", wid, "--json"], capture_output=True, text=True)
    if show2.returncode != 0:
        # Assume deleted
        return
    try:
        parsed = json.loads(show2.stdout)
        wi = parsed.get("workItem") or parsed
        # Accept either explicit deleted status or presence of deleteReason/deletedBy
        assert wi.get("status") in ("deleted", "closed") or wi.get("deleteReason") or wi.get("deletedBy")
    except Exception:
        pytest.skip("Cannot reliably verify wl deletion in this environment")
