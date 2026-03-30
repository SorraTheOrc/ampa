import datetime as dt
import json
import subprocess

from ampa.selection import (
    select_candidate,
    normalize_candidates,
    _candidate_content_hash,
    _apply_content_dedup,
    CandidateHashCache,
)


def _make_proc(payload, returncode=0):
    stdout = json.dumps(payload)
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def test_selection_returns_first_candidate():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc(
            {
                "items": [
                    {
                        "id": "SA-2",
                        "status": "open",
                        "priority": 1,
                        "updated_at": "2026-02-01T00:00:00+00:00",
                    },
                    {
                        "id": "SA-1",
                        "status": "open",
                        "priority": 1,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                ]
            }
        )

    selected = select_candidate(run_shell=run_shell)
    assert selected is not None
    assert selected["id"] == "SA-2"


def test_selection_returns_first_even_if_blocked():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc(
            {
                "items": [
                    {"id": "SA-1", "status": "open", "priority": 2, "blocked": True},
                    {
                        "id": "SA-2",
                        "status": "ready",
                        "priority": 2,
                        "tags": ["skip"],
                    },
                    {"id": "SA-3", "status": "open", "priority": 1},
                ]
            }
        )

    selected = select_candidate(run_shell=run_shell)
    assert selected is not None
    assert selected["id"] == "SA-1"


def test_selection_returns_none_when_empty():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc({"items": []})

    selected = select_candidate(run_shell=run_shell)
    assert selected is None


# ---------------------------------------------------------------------------
# _candidate_content_hash tests
# ---------------------------------------------------------------------------


def test_content_hash_deterministic():
    """Same candidate always yields the same hash."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    assert _candidate_content_hash(item) == _candidate_content_hash(item)


def test_content_hash_order_independent():
    """Key insertion order does not affect the hash."""
    item_a = {"id": "SA-1", "title": "Test", "status": "open"}
    item_b = {"status": "open", "title": "Test", "id": "SA-1"}
    assert _candidate_content_hash(item_a) == _candidate_content_hash(item_b)


def test_content_hash_differs_for_different_content():
    """Different candidate content produces a different hash."""
    item_a = {"id": "SA-1", "title": "Version A"}
    item_b = {"id": "SA-1", "title": "Version B"}
    assert _candidate_content_hash(item_a) != _candidate_content_hash(item_b)


def test_content_hash_differs_for_different_ids():
    """Different IDs produce different hashes even if other fields match."""
    item_a = {"id": "SA-1", "title": "Same title"}
    item_b = {"id": "SA-2", "title": "Same title"}
    assert _candidate_content_hash(item_a) != _candidate_content_hash(item_b)


# ---------------------------------------------------------------------------
# _apply_content_dedup tests
# ---------------------------------------------------------------------------


def test_apply_content_dedup_removes_exact_duplicate():
    item = {"id": "SA-1", "title": "Test"}
    result = _apply_content_dedup([item.copy(), item.copy()])
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_apply_content_dedup_preserves_first_occurrence():
    item1 = {"id": "SA-1", "order": 1}
    item2 = {"id": "SA-2", "order": 2}
    item1_dup = {"id": "SA-1", "order": 1}
    result = _apply_content_dedup([item1, item2, item1_dup])
    assert len(result) == 2
    assert result[0]["id"] == "SA-1"
    assert result[1]["id"] == "SA-2"


def test_apply_content_dedup_keeps_different_content():
    item_a = {"id": "SA-1", "title": "A"}
    item_b = {"id": "SA-1", "title": "B"}
    result = _apply_content_dedup([item_a, item_b])
    assert len(result) == 2


def test_apply_content_dedup_empty_list():
    assert _apply_content_dedup([]) == []


# ---------------------------------------------------------------------------
# normalize_candidates: content-hash dedup across all WL JSON shapes
# ---------------------------------------------------------------------------


def test_normalize_deduplicates_list_exact_duplicates():
    """Plain list shape: exact-duplicate entries are collapsed to one."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    result = normalize_candidates([item.copy(), item.copy()])
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_normalize_deduplicates_results_wrapper_exact_duplicates():
    """results-wrapper shape: exact-duplicate entries are collapsed."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    result = normalize_candidates({"results": [item.copy(), item.copy()]})
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_normalize_deduplicates_items_key_exact_duplicates():
    """items-key shape: exact-duplicate entries are collapsed."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    result = normalize_candidates({"items": [item.copy(), item.copy()]})
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_normalize_deduplicates_candidates_key_exact_duplicates():
    """candidates-key shape: exact-duplicate entries are collapsed."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    result = normalize_candidates({"candidates": [item.copy(), item.copy()]})
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_normalize_deduplicates_anonymous_same_content():
    """Anonymous items (no ID) with identical content are collapsed."""
    item = {"title": "Task", "message": "do X"}
    result = normalize_candidates([item.copy(), item.copy()])
    assert len(result) == 1


def test_normalize_preserves_anonymous_different_content():
    """Anonymous items (no ID) with different content are NOT deduped."""
    item_a = {"title": "Task A", "message": "do A"}
    item_b = {"title": "Task B", "message": "do B"}
    result = normalize_candidates([item_a, item_b])
    assert len(result) == 2


def test_normalize_preserves_different_content_same_id():
    """Items with same ID but different content: only first is kept (ID dedup)."""
    item_a = {"id": "SA-1", "title": "Version A"}
    item_b = {"id": "SA-1", "title": "Version B"}
    result = normalize_candidates([item_a, item_b])
    # ID-based dedup collapses to first; content-hash dedup is a no-op here
    assert len(result) == 1
    assert result[0]["title"] == "Version A"


def test_normalize_single_workitem_shape_not_deduped():
    """Single work-item at top-level is returned as-is (nothing to dedup)."""
    item = {"id": "SA-1", "title": "Test"}
    result = normalize_candidates({"workItem": item})
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


def test_normalize_preserves_distinct_candidates():
    """Distinct candidates with the same key shape are all preserved."""
    items = [
        {"id": "SA-1", "title": "Alpha"},
        {"id": "SA-2", "title": "Beta"},
        {"id": "SA-3", "title": "Gamma"},
    ]
    result = normalize_candidates(items)
    assert len(result) == 3


def test_normalize_wrapped_workitem_list_dedup():
    """Exact-duplicate wrapped-workItem entries in a list are collapsed."""
    inner = {"id": "SA-1", "title": "Test"}
    wrapped = {"workItem": inner}
    result = normalize_candidates([dict(wrapped), dict(wrapped)])
    # After unwrapping, both yield the same inner dict → content dedup collapses
    assert len(result) == 1
    assert result[0]["id"] == "SA-1"


# ---------------------------------------------------------------------------
# CandidateHashCache unit tests
# ---------------------------------------------------------------------------


def test_cache_new_hash_is_not_duplicate():
    cache = CandidateHashCache()
    assert cache.is_duplicate("abc123") is False


def test_cache_is_duplicate_after_mark_seen():
    cache = CandidateHashCache()
    cache.mark_seen("abc123")
    assert cache.is_duplicate("abc123") is True


def test_cache_different_hashes_not_confused():
    cache = CandidateHashCache()
    cache.mark_seen("aaa")
    assert cache.is_duplicate("bbb") is False


def test_cache_to_dict_roundtrip():
    cache = CandidateHashCache()
    cache.mark_seen("h1")
    cache.mark_seen("h2")
    snapshot = cache.to_dict()
    restored = CandidateHashCache.from_dict(snapshot)
    assert restored.is_duplicate("h1")
    assert restored.is_duplicate("h2")
    assert not restored.is_duplicate("h3")


def test_cache_from_dict_ignores_non_string_values():
    data = {"valid_hash": "2026-01-01T00:00:00+00:00", "bad": 42}
    cache = CandidateHashCache.from_dict(data)
    assert cache.is_duplicate("valid_hash")
    assert not cache.is_duplicate("bad")


def test_cache_evicts_expired_entries():
    # Pre-populate with an entry that is already expired (past TTL)
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
    cache = CandidateHashCache.from_dict({"stale_hash": past}, ttl_seconds=86400)
    # Trigger eviction by marking a new entry
    cache.mark_seen("new_hash")
    assert not cache.is_duplicate("stale_hash"), "Expired entry should be evicted"
    assert cache.is_duplicate("new_hash")


def test_cache_evicts_oldest_when_max_size_exceeded():
    # Fill cache to max_size=3, then add a 4th
    past_base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    entries = {
        f"h{i}": (past_base + dt.timedelta(minutes=i)).isoformat() for i in range(3)
    }
    cache = CandidateHashCache.from_dict(entries, max_size=3)
    cache.mark_seen("h_new")
    assert len(cache.to_dict()) == 3, "Cache should not exceed max_size"
    assert cache.is_duplicate("h_new"), "Newest entry must be retained"
    assert not cache.is_duplicate("h0"), "Oldest entry should be evicted"


def test_cache_empty_from_dict():
    cache = CandidateHashCache.from_dict({})
    assert cache.to_dict() == {}


# ---------------------------------------------------------------------------
# Cross-cycle dedup via CandidateHashCache in normalize_candidates / _apply_content_dedup
# ---------------------------------------------------------------------------


def test_cross_cycle_dedup_suppresses_previously_seen_candidate():
    """A candidate seen in cycle 1 should be suppressed in cycle 2."""
    item = {"id": "SA-1", "title": "Test", "status": "open"}
    cache = CandidateHashCache()

    # Cycle 1: first appearance passes through and is recorded
    result1 = normalize_candidates([item.copy()], hash_cache=cache)
    assert len(result1) == 1

    # Cycle 2: same candidate should now be suppressed
    result2 = normalize_candidates([item.copy()], hash_cache=cache)
    assert len(result2) == 0, "Cross-cycle duplicate must be suppressed"


def test_cross_cycle_dedup_allows_new_candidates():
    """A new candidate (different content) must pass through even when cache is populated."""
    item_a = {"id": "SA-1", "title": "Alpha"}
    item_b = {"id": "SA-2", "title": "Beta"}
    cache = CandidateHashCache()

    # Seed the cache with item_a
    normalize_candidates([item_a.copy()], hash_cache=cache)

    # item_b is new — should not be suppressed
    result = normalize_candidates([item_b.copy()], hash_cache=cache)
    assert len(result) == 1
    assert result[0]["id"] == "SA-2"


def test_cross_cycle_dedup_independent_of_cache_when_none():
    """Without a cache, normalize_candidates falls back to within-payload dedup only."""
    item = {"id": "SA-1", "title": "Test"}
    # Call twice without any cache — each call returns the item
    result1 = normalize_candidates([item.copy()])
    result2 = normalize_candidates([item.copy()])
    assert len(result1) == 1
    assert len(result2) == 1


def test_cross_cycle_dedup_multiple_candidates():
    """Only previously-seen candidates are suppressed; new ones pass through."""
    item_a = {"id": "SA-1", "title": "Alpha"}
    item_b = {"id": "SA-2", "title": "Beta"}
    item_c = {"id": "SA-3", "title": "Gamma"}
    cache = CandidateHashCache()

    # Cycle 1: all three candidates are new
    result1 = normalize_candidates([item_a.copy(), item_b.copy()], hash_cache=cache)
    assert len(result1) == 2

    # Cycle 2: item_a and item_b are known; item_c is new
    result2 = normalize_candidates(
        [item_a.copy(), item_b.copy(), item_c.copy()], hash_cache=cache
    )
    assert len(result2) == 1
    assert result2[0]["id"] == "SA-3"


def test_apply_content_dedup_cross_cycle_via_cache():
    """Direct test of _apply_content_dedup cross-cycle layer."""
    item = {"id": "SA-1", "status": "open"}
    cache = CandidateHashCache()

    result1 = _apply_content_dedup([item.copy()], hash_cache=cache)
    assert len(result1) == 1

    result2 = _apply_content_dedup([item.copy()], hash_cache=cache)
    assert len(result2) == 0, "Cross-cycle dedup must suppress the second occurrence"
