"""Candidate selection service for WL work items."""

from __future__ import annotations
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("ampa.selection")

# Number of candidates to request from `wl next` by default. Can be overridden
# via the environment variable AMPA_WL_NEXT_COUNT. This ensures the scheduler
# sees multiple candidates (not just the single top candidate) and can try
# fallbacks if the top candidate is unsupported.
WL_NEXT_DEFAULT_COUNT = int(os.getenv("AMPA_WL_NEXT_COUNT", "3"))


# ---------------------------------------------------------------------------
# Content-hash helpers for exact-report deduplication
# ---------------------------------------------------------------------------


def _candidate_content_hash(candidate: Dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hex digest of a candidate dict.

    Two candidates with identical content (same keys and values, in any
    insertion order) produce the same hash.  Used for exact-report
    deduplication at ingestion: O(1) per candidate with a set lookup.
    """
    serialized = json.dumps(candidate, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class CandidateHashCache:
    """Bounded TTL cache of candidate content hashes for cross-cycle dedup.

    Persists seen candidate hashes between scheduler poll cycles so that
    identical reports emitted in separate ``wl next`` invocations are
    suppressed on subsequent ingestion passes.

    Memory is bounded by two independent mechanisms:
    - **TTL**: entries older than *ttl_seconds* are evicted on every write.
    - **Max-size**: when the cache exceeds *max_size* the oldest entries
      (by timestamp) are dropped, keeping the most-recent *max_size* hashes.

    The cache is serialisable to/from a plain ``dict[str, str]``
    (``{hash_hex: iso_timestamp}``) for storage in the scheduler state file.
    """

    DEFAULT_MAX_SIZE: int = 200
    DEFAULT_TTL_SECONDS: int = 86400  # 24 hours

    def __init__(
        self,
        entries: Optional[Dict[str, str]] = None,
        max_size: int = DEFAULT_MAX_SIZE,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._entries: Dict[str, str] = dict(entries) if entries else {}
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

    # -- public interface ---------------------------------------------------

    def is_duplicate(self, hash_str: str) -> bool:
        """Return True if *hash_str* was marked seen and has not expired."""
        return hash_str in self._entries

    def mark_seen(self, hash_str: str) -> None:
        """Record *hash_str* as seen (now) and evict stale/overflow entries."""
        self._entries[hash_str] = dt.datetime.now(dt.timezone.utc).isoformat()
        self._evict()

    def to_dict(self) -> Dict[str, str]:
        """Return a JSON-safe ``{hash: iso_timestamp}`` snapshot."""
        return dict(self._entries)

    @classmethod
    def from_dict(
        cls,
        data: Any,
        max_size: int = DEFAULT_MAX_SIZE,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> "CandidateHashCache":
        """Reconstruct a cache from a ``to_dict()`` snapshot."""
        entries: Dict[str, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    entries[k] = v
        return cls(entries=entries, max_size=max_size, ttl_seconds=ttl_seconds)

    # -- eviction -----------------------------------------------------------

    def _evict(self) -> None:
        """Remove expired entries then trim to *max_size* (oldest first)."""
        now = dt.datetime.now(dt.timezone.utc)
        expired = []
        for h, ts_str in self._entries.items():
            try:
                ts = dt.datetime.fromisoformat(ts_str)
                if not ts.tzinfo:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if (now - ts).total_seconds() > self.ttl_seconds:
                    expired.append(h)
            except Exception:
                expired.append(h)
        for h in expired:
            del self._entries[h]
        # Trim to max_size by removing the oldest (smallest) timestamps
        if len(self._entries) > self.max_size:
            sorted_entries = sorted(self._entries.items(), key=lambda x: x[1])
            n_remove = len(self._entries) - self.max_size
            for h, _ in sorted_entries[:n_remove]:
                del self._entries[h]


def _apply_content_dedup(
    candidates: List[Dict[str, Any]],
    hash_cache: Optional[CandidateHashCache] = None,
) -> List[Dict[str, Any]]:
    """Remove exact-duplicate candidates using content hashing.

    Performs two layers of deduplication:
    1. **Within-payload**: suppresses candidates that appear more than once
       in the current *candidates* list (ephemeral, per-call ``set``).
    2. **Cross-cycle** (when *hash_cache* is supplied): suppresses candidates
       whose hash was recorded in a previous ingestion pass and has not yet
       expired.  New candidates are registered in *hash_cache* so they are
       suppressed on the *next* cycle if the same payload is returned again.

    Preserves the first occurrence of each unique candidate (by content)
    and drops subsequent identical entries.  Operates in O(n) time with
    O(n) memory, bounded by the number of candidates in *candidates*.
    """
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for candidate in candidates:
        h = _candidate_content_hash(candidate)
        if h in seen:
            LOG.debug(
                "Suppressing exact-duplicate candidate within payload (content_hash=%s)",
                h[:12],
            )
            continue
        if hash_cache is not None and hash_cache.is_duplicate(h):
            LOG.debug(
                "Suppressing cross-cycle duplicate candidate (content_hash=%s)", h[:12]
            )
            continue
        seen.add(h)
        if hash_cache is not None:
            hash_cache.mark_seen(h)
        unique.append(candidate)
    return unique


class WLNextClient:
    def __init__(
        self,
        run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        command_cwd: Optional[str] = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.run_shell = run_shell or subprocess.run
        self.command_cwd = command_cwd
        self.timeout_seconds = timeout_seconds

    def fetch_payload(self) -> Optional[Dict[str, Any]]:
        # Request multiple candidates so the delegation code can iterate past
        # the top candidate if it's not actionable. Use the configurable
        # AMPA_WL_NEXT_COUNT env var to control how many to request.
        count = WL_NEXT_DEFAULT_COUNT
        cmd = f"wl next -n {count} --json"

        def _run(cmd_str: str) -> Optional[subprocess.CompletedProcess]:
            try:
                LOG.debug("Running wl next command: %s", cmd_str)
                proc = self.run_shell(
                    cmd_str,
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                    timeout=self.timeout_seconds,
                )
                return proc
            except Exception:
                LOG.exception("Failed running wl next")
                return None

        proc = _run(cmd)
        used_count = True
        if proc is not None and getattr(proc, "returncode", 1) == 0:
            if not (getattr(proc, "stdout", None) or "").strip():
                proc = None
        # Log proc output for debugging test failures where a fake runner may
        # return an unexpected CompletedProcess shape or empty stdout.
        if proc is not None:
            LOG.debug(
                "wl next initial result rc=%s stdout=%r stderr=%r cmd=%r",
                getattr(proc, "returncode", None),
                (getattr(proc, "stdout", None) or "")[:2048],
                (getattr(proc, "stderr", None) or "")[:2048],
                cmd,
            )

        # Compatibility fallback: some WL installations do not accept '-n'. If
        # the initial invocation failed, try the simpler form `wl next --json`.
        if proc is None or getattr(proc, "returncode", 1) != 0:
            if proc is not None:
                LOG.debug(
                    "wl next (with -n) failed rc=%s stderr=%r",
                    getattr(proc, "returncode", None),
                    (getattr(proc, "stderr", None) or "")[:512],
                )
            # try without -n
            cmd2 = "wl next --json"
            proc2 = _run(cmd2)
            if proc2 is not None:
                LOG.debug(
                    "wl next fallback result rc=%s stdout=%r stderr=%r cmd=%r",
                    getattr(proc2, "returncode", None),
                    (getattr(proc2, "stdout", None) or "")[:2048],
                    (getattr(proc2, "stderr", None) or "")[:2048],
                    cmd2,
                )
            if proc2 is None or getattr(proc2, "returncode", 1) != 0:
                if proc2 is not None:
                    LOG.warning(
                        "wl next fallback failed rc=%s stderr=%r",
                        getattr(proc2, "returncode", None),
                        (getattr(proc2, "stderr", None) or "")[:512],
                    )
                return None
            proc = proc2
            used_count = False

        stdout = getattr(proc, "stdout", None) or ""
        if not stdout.strip():
            LOG.warning("wl next returned empty output")
            # Fallback: if we requested multiple candidates with -n, some
            # WL versions may only support the simpler form. Try again with
            # `wl next --json` as a compatibility fallback.
            if used_count:
                proc2 = _run("wl next --json")
                if (
                    proc2
                    and getattr(proc2, "returncode", 1) == 0
                    and getattr(proc2, "stdout", "").strip()
                ):
                    LOG.debug(
                        "wl next fallback (no -n) stdout=%r",
                        (getattr(proc2, "stdout", None) or "")[:2048],
                    )
                    try:
                        payload = json.loads(proc2.stdout)
                    except Exception:
                        LOG.warning(
                            "wl next fallback returned invalid JSON payload=%r",
                            (proc2.stdout or "")[:1024],
                        )
                        return None
                    return payload if isinstance(payload, dict) else {"items": payload}
            return None
        try:
            payload = json.loads(stdout)
        except Exception:
            LOG.warning("wl next returned invalid JSON payload=%r", stdout[:1024])
            # If parsing failed for the -n invocation, try the no- -n form once
            # more as a last resort (handles implementations that emit slightly
            # different JSON shapes).
            if used_count:
                proc2 = _run("wl next --json")
                if proc2 is not None:
                    LOG.debug(
                        "wl next retry fallback result rc=%s stdout=%r stderr=%r cmd=%r",
                        getattr(proc2, "returncode", None),
                        (getattr(proc2, "stdout", None) or "")[:2048],
                        (getattr(proc2, "stderr", None) or "")[:2048],
                        "wl next --json",
                    )
                if (
                    proc2
                    and getattr(proc2, "returncode", 1) == 0
                    and getattr(proc2, "stdout", "").strip()
                ):
                    try:
                        payload = json.loads(proc2.stdout)
                    except Exception:
                        LOG.warning(
                            "wl next fallback returned invalid JSON payload=%r",
                            (proc2.stdout or "")[:1024],
                        )
                        return None
                    return payload if isinstance(payload, dict) else {"items": payload}
            return None
        return payload if isinstance(payload, dict) else {"items": payload}


def _normalize_candidates(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        # Normalize a plain list of work-item-like dicts. Also accept lists where
        # each element wraps a work item under keys like 'workItem'.
        out: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            # unwrap common wrapper key
            inner = None
            for k in ("workItem", "work_item", "item"):
                v = item.get(k)
                if isinstance(v, dict):
                    inner = v
                    break
            out.append(inner or item)
        # dedupe by id preserving order
        seen = set()
        unique: List[Dict[str, Any]] = []
        for it in out:
            wid = (
                it.get("id")
                or it.get("work_item_id")
                or it.get("workItemId")
                or it.get("workItem")
            )
            key = str(wid) if wid is not None else None
            if key is None:
                unique.append(it)
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(it)
        return unique
    if not isinstance(payload, dict):
        return []
    # Some WL implementations return a top-level 'results' list where each
    # entry may wrap the actual work item under 'workItem'. Handle that first.
    if isinstance(payload.get("results"), list):
        out: List[Dict[str, Any]] = []
        for entry in payload.get("results", []):
            if not isinstance(entry, dict):
                continue
            # prefer an explicit wrapped workItem when present
            inner = None
            for k in ("workItem", "work_item", "item"):
                v = entry.get(k)
                if isinstance(v, dict):
                    inner = v
                    break
            out.append(inner or entry)
        # dedupe by id preserving order
        seen = set()
        unique: List[Dict[str, Any]] = []
        for it in out:
            wid = (
                it.get("id")
                or it.get("work_item_id")
                or it.get("workItemId")
                or it.get("workItem")
            )
            key = str(wid) if wid is not None else None
            if key is None:
                unique.append(it)
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(it)
        return unique

    for key in ("candidates", "workItems", "work_items", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            # unwrap elements that are wrapper objects
            out: List[Dict[str, Any]] = []
            for item in val:
                if not isinstance(item, dict):
                    continue
                inner = None
                for k in ("workItem", "work_item", "item"):
                    v = item.get(k)
                    if isinstance(v, dict):
                        inner = v
                        break
                out.append(inner or item)
            return out

    # Single work-item at top-level
    for key in ("workItem", "work_item", "item"):
        val = payload.get(key)
        if isinstance(val, dict):
            return [val]

    return []


def normalize_candidates(
    payload: Any,
    hash_cache: Optional[CandidateHashCache] = None,
) -> List[Dict[str, Any]]:
    """Normalize and deduplicate candidates from a WL next payload.

    Applies both ID-based deduplication (in ``_normalize_candidates``) and
    exact-report content-hash deduplication so that identical candidates
    emitted multiple times are presented only once to the scheduler.

    When *hash_cache* is supplied, an additional cross-cycle dedup layer
    filters candidates that were already seen in a previous poll cycle.
    New candidates are recorded in *hash_cache* (mutated in-place) for
    suppression on subsequent cycles.
    """
    return _apply_content_dedup(_normalize_candidates(payload), hash_cache=hash_cache)


def select_candidate(
    *,
    run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    command_cwd: Optional[str] = None,
    timeout_seconds: int = 10,
) -> Optional[Dict[str, Any]]:
    client = WLNextClient(
        run_shell=run_shell,
        command_cwd=command_cwd,
        timeout_seconds=timeout_seconds,
    )
    payload = client.fetch_payload()
    candidates = normalize_candidates(payload)
    if not candidates:
        return None

    return candidates[0]


def fetch_candidates(
    *,
    run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    command_cwd: Optional[str] = None,
    timeout_seconds: int = 10,
    hash_cache: Optional[CandidateHashCache] = None,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Fetch and normalize candidates from ``wl next``.

    Parameters
    ----------
    run_shell:
        Optional callable that executes shell commands (for testing).
    command_cwd:
        Working directory for the ``wl next`` invocation.
    timeout_seconds:
        Seconds to wait before aborting the ``wl next`` call.
    hash_cache:
        Optional :class:`CandidateHashCache` instance.  When supplied,
        candidates whose content hash was recorded in a previous call are
        suppressed (cross-cycle dedup).  New candidates are registered in
        the cache (mutated in-place) for suppression on the next cycle.
    """
    client = WLNextClient(
        run_shell=run_shell,
        command_cwd=command_cwd,
        timeout_seconds=timeout_seconds,
    )
    payload = client.fetch_payload()
    return normalize_candidates(payload, hash_cache=hash_cache), payload
