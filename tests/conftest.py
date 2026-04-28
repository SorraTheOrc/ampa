import subprocess
import json
import pytest
from typing import Optional

TEST_PREFIX = "TEST-CI-"

@pytest.fixture
def wl_test_items(request):
    """Helper fixture to create worklog test items and ensure cleanup after the test.

    Returns a factory function create(suffix) -> dict(json response from `wl create`).
    The fixture registers a finalizer that will attempt to delete any work items whose
    title begins with TEST_PREFIX to avoid accidental deletion of production items.
    """
    created_ids = []

    def create(suffix: Optional[str] = None):
        title = f"{TEST_PREFIX}{suffix or request.node.name}"
        cmd = ["wl", "create", "--title", title, "--json"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"wl create failed: {res.stderr}")
        try:
            data = json.loads(res.stdout)
        except Exception:
            # Fallback: return raw stdout if JSON parsing fails
            data = {"raw": res.stdout}
        # Attempt to extract id if present
        wid = None
        if isinstance(data, dict):
            wid = data.get("id") or (data.get("workItem") or {}).get("id")
        if wid:
            created_ids.append(wid)
        return data

    def _finalize():
        # Search for any remaining test-prefixed items and delete them.
        try:
            search = subprocess.run(["wl", "search", TEST_PREFIX, "--json"], capture_output=True, text=True)
            if search.returncode == 0 and search.stdout.strip():
                try:
                    items = json.loads(search.stdout)
                except Exception:
                    items = []
                ids = []
                for it in items:
                    if isinstance(it, dict):
                        if it.get("title", "").startswith(TEST_PREFIX):
                            ids.append(it.get("id"))
                # Ensure we also include any known created ids
                ids.extend(x for x in created_ids if x not in ids)
                # Deduplicate
                ids = list(dict.fromkeys([i for i in ids if i]))
                for wid in ids:
                    subprocess.run(["wl", "delete", wid])
        except FileNotFoundError:
            # `wl` not available in this environment; nothing to do
            pass

    request.addfinalizer(_finalize)
    return create


# ------------------------------------------------------------------
# Session-level cleanup: ensure no lingering WL- prefixed work items
# remain in the Worklog database after the test session. Some older
# tests or external tools may create WL- ID work items; to enforce the
# project policy we delete any WL- items at session end. This uses the
# `wl` CLI so the deletion is recorded properly in Worklog.
# ------------------------------------------------------------------

def pytest_sessionfinish(session, exitstatus):
    try:
        res = subprocess.run(["wl", "search", "WL-", "--json"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            try:
                items = json.loads(res.stdout)
            except Exception:
                items = []
            ids = []
            for it in items:
                if isinstance(it, dict):
                    wid = it.get("id")
                    if wid and wid.startswith("WL-"):
                        ids.append(wid)
            # Deduplicate and delete
            ids = list(dict.fromkeys(ids))
            for wid in ids:
                subprocess.run(["wl", "delete", wid, "--json"], capture_output=True)
    except FileNotFoundError:
        # `wl` not available; nothing we can do
        pass
