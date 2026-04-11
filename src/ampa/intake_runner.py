"""Intake runner to query and select idea-stage work items.

This module provides a small runner used by the Scheduler when a command
with command_type=="intake" (or id == "intake-selector") is executed.
It delegates to IntakeCandidateSelector to query `wl list --stage idea` and
select a single top candidate. When a candidate is chosen it records a
per-item `last_selected_at_by_item` timestamp in the scheduler store so
that subsequent cycles can observe recent selections.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .intake_selector import IntakeCandidateSelector
from . import notifications

LOG = logging.getLogger("ampa.intake_runner")


class IntakeRunner:
    def __init__(self, run_shell: Any, command_cwd: str):
        self.run_shell = run_shell
        self.command_cwd = command_cwd

    def run(self, spec, store) -> dict:
        """Run one intake selection cycle.

        Returns a small dict describing outcome: {"selected": id or None}.
        """
        selector = IntakeCandidateSelector(run_shell=self.run_shell, cwd=self.command_cwd)
        candidates = selector.query_candidates()
        if candidates is None:
            LOG.warning("Intake runner: wl list query failed")
            return {"selected": None, "error": "query_failed"}
        if not candidates:
            LOG.info("Intake runner: no idea-stage candidates")
            return {"selected": None}

        selected = selector.select_top(candidates)
        if selected is None:
            LOG.info("Intake runner: no selection made")
            return {"selected": None}

        wid = str(selected.get("id") or "")
        if not wid:
            LOG.warning("Intake runner: selected candidate missing id")
            return {"selected": None}

        LOG.info("Intake runner: selected candidate %s — %s", wid, selected.get("title") or selected.get("name") or "(no title)")

        # Persist selection timestamp to store state for observability.
        try:
            state = store.get_state(spec.command_id) or {}
            state.setdefault("last_selected_at_by_item", {})
            state["last_selected_at_by_item"][wid] = dt.datetime.now(dt.timezone.utc).isoformat()
            store.update_state(spec.command_id, state)
        except Exception:
            LOG.exception("Failed to persist intake selection state for %s", wid)

        # Integration: notify operators via Discord that an intake candidate
        # was selected and add a TODO comment to the work item indicating
        # that the intake dispatch has not been implemented yet. The real
        # intake dispatch mechanism (AM-0MNUB4V80006OGMK) will be implemented
        # in a follow-up change.
        try:
            title_text = selected.get("title") or selected.get("name") or "(no title)"
            notif_title = "Automated Intake Selected"
            notif_body = f"{title_text} ({wid}) has been selected for automated intake processing.\n\nTODO: intake dispatch not implemented yet."
            # message_type 'intake' used for state tracking; callers may
            # suppress Discord in CI via AMPA_DISABLE_DISCORD.
            try:
                notifications.notify(notif_title, notif_body, message_type="intake")
            except Exception:
                LOG.exception("Failed to send intake notification for %s", wid)
        except Exception:
            LOG.exception("Failed to build/send intake notification for %s", wid)

        # Add a TODO comment to the work item so humans see the selection
        # in Worklog. Use the wl CLI via the injected run_shell. This is a
        # best-effort operation; failures are logged but do not abort the
        # selection flow.
        try:
            comment_text = (
                f"TODO: Automated intake selected by AMPA. "
                "Intake dispatch not implemented yet."
            )
            cmd = f"wl comment add {wid} --comment \"{comment_text}\" --author \"ampa\" --json"
            try:
                # best-effort call; do not raise on non-zero exit
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd, timeout=60)
            except TypeError:
                # Some test doubles may not accept timeout kwarg
                self.run_shell(cmd, shell=True, check=False, capture_output=True, text=True, cwd=self.command_cwd)
        except Exception:
            LOG.exception("Failed to add Worklog comment for selected intake candidate %s", wid)
        return {"selected": wid}
