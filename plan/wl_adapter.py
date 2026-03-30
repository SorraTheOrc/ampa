from __future__ import annotations

import json
import shlex
from subprocess import CalledProcessError, check_output
from typing import Any, Dict, List, Optional


class WLAdapter:
    """Thin adapter around the `wl` CLI used by tests and local runs.

    This adapter shells out to the `wl` program. It keeps behavior permissive: when
    a command fails (wl missing or API not available) methods return None or an
    empty list rather than raising, so callers can decide how strict to be.
    """

    def _run(self, args: List[str]) -> Optional[str]:
        cmd = ["wl"] + args
        try:
            out = check_output(cmd, encoding="utf-8")
            return out
        except FileNotFoundError:
            return None
        except CalledProcessError:
            # permissive: return None on failure
            return None

    def list_children(self, parent: str) -> List[Dict[str, Any]]:
        out = self._run(["list", "--parent", parent, "--json"])
        if not out:
            return []
        try:
            return json.loads(out)
        except Exception:
            return []

    def dep_add(self, blocked: str, blocker: str) -> bool:
        # request machine-readable output from wl to make callers able to
        # parse/inspect responses if needed
        out = self._run(["dep", "add", blocked, blocker, "--json"])
        return out is not None

    def dep_rm(self, blocked: str, blocker: str) -> bool:
        out = self._run(["dep", "rm", blocked, blocker, "--json"])
        return out is not None

    def dep_list(self, id: str) -> List[Dict[str, Any]]:
        out = self._run(["dep", "list", id, "--json"])
        if not out:
            return []
        try:
            return json.loads(out)
        except Exception:
            return []

    def post_comment(self, id: str, text: str) -> bool:
        # quote the body and use wl comment add
        # wl comment add <id> --body "text"
        out = self._run(["comment", "add", id, "--body", text])
        return out is not None

    def show(self, id: str) -> Optional[Dict[str, Any]]:
        out = self._run(["show", id, "--json"])
        if not out:
            return None
        try:
            return json.loads(out)
        except Exception:
            return None

    def detect_existing_comment_exact(self, id: str, text: str) -> bool:
        w = self.show(id)
        if not w:
            return False
        comments = w.get("comments") or []
        for c in comments:
            if c.get("body") == text:
                return True
        return False

    def delete_comment(self, work_id: str, comment_id: str) -> bool:
        """Delete a comment and verify it is removed.

        Args:
            work_id: work item id (e.g. "SA-0XXX...")
            comment_id: comment identifier portion (e.g. "C1") or full form
                including work item prefix (e.g. "SA-0XXX-C1").

        Returns:
            True if deletion was successful and subsequent show no longer
            lists the comment, False otherwise.
        """
        # Accept either a full comment ref (SA-...-C1) or just the tail (C1).
        if comment_id.startswith(work_id):
            ref = comment_id
        elif "-" in comment_id and comment_id.split("-", 1)[0].startswith("SA-"):
            # already looks like a full ref
            ref = comment_id
        else:
            ref = f"{work_id}-{comment_id}"

        out = self._run(["comment", "delete", ref])
        # If the delete invocation failed at the CLI layer, report failure.
        if out is None:
            return False

        # Verify by fetching the work item and ensuring the comment is absent.
        w = self.show(work_id)
        if not w:
            # Unable to fetch work item state to verify; treat as failure so
            # callers don't assume deletion when data is ambiguous.
            return False

        # normalize different possible wl show outputs into a comments list
        comments = []
        try:
            if isinstance(w, dict):
                # top-level comments
                if isinstance(w.get("comments"), list):
                    comments = w.get("comments")
                else:
                    # common wrappers: workItem, work_item, data, items
                    for key in ("workItem", "work_item", "data", "items"):
                        val = w.get(key)
                        if isinstance(val, dict):
                            # inner dict may contain comments or items
                            cand = (
                                val.get("comments")
                                or val.get("items")
                                or val.get("data")
                            )
                            if isinstance(cand, list):
                                comments = cand
                                break
                        if isinstance(val, list):
                            # val itself may be a list of comments/items
                            comments = val
                            break
        except Exception:
            comments = []

        def _matches(c: dict) -> bool:
            # comment id may appear under several keys depending on WL variant
            cid = c.get("id") or c.get("commentId") or c.get("comment_id")
            if cid and (
                str(cid) == comment_id
                or str(cid) == ref
                or str(cid).endswith(str(comment_id))
            ):
                return True
            # some WL variants include the full ref in a separate field
            for key in ("ref", "reference"):
                v = c.get(key)
                if v and (str(v) == ref or str(v).endswith(str(comment_id))):
                    return True
            return False

        for c in comments or []:
            if _matches(c):
                # still present
                return False
        return True
