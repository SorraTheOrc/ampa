"""PR monitor scheduled command.

Periodically enumerates all open pull requests in the repository, checks
their CI status (required check runs / statuses), and takes action:

* **All required checks passing** — post a Worklog comment and a GitHub
  PR comment indicating the PR is "ready for review" (only once per PR
  to avoid noise).
* **Required checks failing** — post a Worklog comment and create a
  critical Worklog work item linking to the PR and failing checks.

The command uses the ``gh`` CLI for GitHub API access.  If ``gh`` is not
available the runner logs a clear error and exits gracefully.

Configuration
-------------
Behaviour is driven from ``CommandSpec.metadata``:

* ``dedup`` (bool, default ``True``) — when true the runner will not
  re-post a "ready for review" comment if one already exists on the PR.
* ``max_prs`` (int, default ``50``) — maximum number of open PRs to
  evaluate per run (to avoid hitting API rate limits).
* ``gh_command`` (str, default ``"gh"``) — path or name of the ``gh``
  CLI binary.

The command does **not** require LLM availability.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("ampa.pr_monitor")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PRS: int = 50
_DEFAULT_GH_COMMAND: str = "gh"
_READY_COMMENT_MARKER: str = "<!-- ampa-pr-monitor:ready -->"
_FAILURE_COMMENT_MARKER: str = "<!-- ampa-pr-monitor:failure -->"
_AUDIT_RESULT_MARKER: str = "<!-- ampa-pr-audit-result -->"
_AUDIT_DISPATCH_MARKER_PREFIX: str = "<!-- ampa-pr-audit-dispatch:"

# Pattern to extract work-item IDs from branch names.
# Matches: feature/<ID>-*, bug/<ID>-*, wl-<ID>-*, or bare <PREFIX>-<HASH>
_WORK_ITEM_ID_BRANCH_RE = re.compile(
    r"(?:feature/|bug/|wl-)?((?:[A-Z]{2,}-)?[A-Za-z0-9]{10,})"
)
# Pattern to extract work-item IDs from PR body markers.
_WORK_ITEM_ID_BODY_RE = re.compile(
    r"(?:work[- ]?item|closes|fixes|resolves)[:\s]+([A-Z]{2,}-[A-Za-z0-9]*[0-9][A-Za-z0-9]*)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# PRMonitorRunner
# ---------------------------------------------------------------------------


class PRMonitorRunner:
    """Run the PR monitor scheduled command.

    Parameters
    ----------
    run_shell:
        Callable with the same signature as :func:`subprocess.run`.
    command_cwd:
        Working directory for shell commands.
    notifier:
        Object with a ``notify(title, body, message_type)`` method used
        for Discord notifications.
    wl_shell:
        Optional separate callable for ``wl`` commands.  Defaults to
        *run_shell*.
    dispatcher:
        Optional :class:`~ampa.engine.dispatch.Dispatcher` used to
        spawn LLM audit sessions when ``auto_review`` is enabled.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        notifier: Optional[Any] = None,
        wl_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        dispatcher: Optional[Any] = None,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self._notifier = notifier
        self._wl_shell = wl_shell or run_shell
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, spec: Any) -> Dict[str, Any]:
        """Execute one PR-monitor cycle.

        1. Verify ``gh`` CLI is available.
        2. List open PRs via ``gh pr list``.
        3. For each PR, check CI status via ``gh pr checks``.
        4. Post comments / create work items as appropriate.
        5. Return a result dict summarising the run.

        Returns
        -------
        dict
            Keys:
            * ``action`` — ``"completed"``, ``"no_prs"``, ``"gh_unavailable"``,
              ``"list_failed"``
            * ``prs_checked`` — number of PRs evaluated
            * ``ready_prs`` — list of PR numbers marked ready
            * ``failing_prs`` — list of PR numbers with failing CI
            * ``skipped_prs`` — list of PR numbers skipped (already notified)
            * ``note`` — human-readable summary
            * ``open_prs`` — number of open PRs found
            * ``skipped_pending_prs`` — number skipped due to pending checks
            * ``skipped_dedup_prs`` — number skipped due to dedup marker
            * ``checks_unavailable_prs`` — number skipped due to check query errors
            * ``llm_reviews_dispatched`` — number of LLM audits dispatched
            * ``llm_reviews_presented`` — number of audit results presented in Discord
            * ``notifications_sent`` — number of Discord notifications sent
        """
        self._metrics_reset()
        metadata: Dict[str, Any] = getattr(spec, "metadata", {}) or {}
        gh_cmd = str(metadata.get("gh_command", _DEFAULT_GH_COMMAND))
        dedup = _coerce_bool(metadata.get("dedup", True))
        auto_review = _coerce_bool(metadata.get("auto_review", True))
        try:
            max_prs = int(metadata.get("max_prs", _DEFAULT_MAX_PRS))
        except (TypeError, ValueError):
            max_prs = _DEFAULT_MAX_PRS

        # 1. Check gh availability
        if not self._gh_available(gh_cmd):
            note = "pr-monitor: gh CLI not available — aborting"
            LOG.error(note)
            return {
                "action": "gh_unavailable",
                "prs_checked": 0,
                "open_prs": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "skipped_pending_prs": 0,
                "skipped_dedup_prs": 0,
                "checks_unavailable_prs": 0,
                "llm_reviews_dispatched": 0,
                "llm_reviews_presented": 0,
                "notifications_sent": 0,
                "note": note,
            }

        # 2. List open PRs
        prs = self._list_open_prs(gh_cmd, max_prs)
        if prs is None:
            note = "pr-monitor: failed to list open PRs"
            LOG.error(note)
            return {
                "action": "list_failed",
                "prs_checked": 0,
                "open_prs": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "skipped_pending_prs": 0,
                "skipped_dedup_prs": 0,
                "checks_unavailable_prs": 0,
                "llm_reviews_dispatched": 0,
                "llm_reviews_presented": 0,
                "notifications_sent": 0,
                "note": note,
            }
        if not prs:
            note = "pr-monitor: no open PRs found"
            LOG.info(note)
            return {
                "action": "no_prs",
                "prs_checked": 0,
                "open_prs": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "skipped_pending_prs": 0,
                "skipped_dedup_prs": 0,
                "checks_unavailable_prs": 0,
                "llm_reviews_dispatched": 0,
                "llm_reviews_presented": 0,
                "notifications_sent": 0,
                "note": note,
            }

        # 3. Evaluate each PR
        ready_prs: List[int] = []
        failing_prs: List[int] = []
        skipped_prs: List[int] = []
        skipped_pending_prs = 0
        skipped_dedup_prs = 0
        checks_unavailable_prs = 0

        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue
            pr_number = int(pr_number)
            pr_title = pr.get("title", f"PR #{pr_number}")
            pr_url = pr.get("url", "")

            check_status = self._get_check_status(gh_cmd, pr_number)
            if check_status is None:
                LOG.warning(
                    "pr-monitor: could not retrieve check status for PR #%d",
                    pr_number,
                )
                checks_unavailable_prs += 1
                continue

            all_passing, failing_checks, pending_checks = check_status

            if pending_checks and not failing_checks:
                # Checks still running — skip this PR for now
                LOG.info(
                    "pr-monitor: PR #%d has pending checks — skipping",
                    pr_number,
                )
                skipped_prs.append(pr_number)
                skipped_pending_prs += 1
                continue

            if all_passing:
                # Check for dedup — has a ready comment been posted already?
                if dedup and self._has_existing_comment(
                    gh_cmd, pr_number, _READY_COMMENT_MARKER
                ):
                    LOG.info(
                        "pr-monitor: PR #%d already marked ready — skipping",
                        pr_number,
                    )
                    skipped_prs.append(pr_number)
                    skipped_dedup_prs += 1

                    # Even though we skip the ready comment, check for
                    # pending audit results when auto_review is enabled.
                    if auto_review:
                        self._check_and_present_audit_results(
                            gh_cmd, pr, pr_number, pr_title,
                            pr.get("url", ""),
                        )
                    continue

                self._handle_ready_pr(
                    gh_cmd, pr_number, pr_title, pr_url
                )
                ready_prs.append(pr_number)

                # Dispatch LLM audit when auto_review is enabled
                if auto_review:
                    self._dispatch_review(gh_cmd, pr, pr_number, pr_title)
            elif failing_checks:
                self._handle_failing_pr(
                    gh_cmd, pr_number, pr_title, pr_url, failing_checks
                )
                failing_prs.append(pr_number)

        # Send summary notification (include PR metadata so we can format links)
        self._notify_summary(ready_prs, failing_prs, skipped_prs, len(prs), prs)

        note = (
            f"pr-monitor: checked {len(prs)} PR(s) — "
            f"{len(ready_prs)} ready, {len(failing_prs)} failing, "
            f"{len(skipped_prs)} skipped; "
            f"{self._metrics.get('llm_reviews_dispatched', 0)} LLM dispatched; "
            f"{self._metrics.get('llm_reviews_presented', 0)} LLM presented; "
            f"{self._metrics.get('notifications_sent', 0)} notifications"
        )
        LOG.info(note)

        return {
            "action": "completed",
            "prs_checked": len(prs),
            "open_prs": len(prs),
            "ready_prs": ready_prs,
            "failing_prs": failing_prs,
            "skipped_prs": skipped_prs,
            "skipped_pending_prs": skipped_pending_prs,
            "skipped_dedup_prs": skipped_dedup_prs,
            "checks_unavailable_prs": checks_unavailable_prs,
            "llm_reviews_dispatched": int(
                self._metrics.get("llm_reviews_dispatched", 0)
            ),
            "llm_reviews_presented": int(
                self._metrics.get("llm_reviews_presented", 0)
            ),
            "notifications_sent": int(self._metrics.get("notifications_sent", 0)),
            "auto_review_enabled": auto_review,
            "dedup_enabled": dedup,
            "note": note,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _metrics_reset(self) -> None:
        self._metrics = {
            "llm_reviews_dispatched": 0,
            "llm_reviews_presented": 0,
            "notifications_sent": 0,
        }

    def _metric_inc(self, key: str, value: int = 1) -> None:
        metrics = getattr(self, "_metrics", None)
        if not isinstance(metrics, dict):
            self._metrics_reset()
            metrics = self._metrics
        metrics[key] = int(metrics.get(key, 0)) + int(value)

    def _send_notification(self, payload: Dict[str, Any], message_type: str) -> bool:
        if self._notifier is None:
            return False
        # notifications.notify() requires a positional title argument even when
        # sending a pre-built payload.
        self._notifier.notify(title="", payload=payload, message_type=message_type)
        self._metric_inc("notifications_sent")
        return True

    def _gh_available(self, gh_cmd: str) -> bool:
        """Return True if the gh CLI is available."""
        try:
            proc = self.run_shell(
                f"{gh_cmd} --version",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            return proc.returncode == 0
        except Exception:
            LOG.exception("pr-monitor: exception checking gh availability")
            return False

    def _list_open_prs(
        self, gh_cmd: str, max_prs: int
    ) -> Optional[List[Dict[str, Any]]]:
        """List open PRs using gh CLI.  Returns None on failure."""
        try:
            cmd = (
                f"{gh_cmd} pr list --state open "
                f"--json number,title,url,headRefName,updatedAt "
                f"--limit {max_prs}"
            )
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception("pr-monitor: exception listing open PRs")
            return None

        if proc.returncode != 0:
            LOG.warning(
                "pr-monitor: gh pr list failed rc=%s stderr=%r",
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return None

        stdout = (proc.stdout or "").strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            LOG.warning(
                "pr-monitor: gh pr list returned invalid JSON: %r",
                stdout[:512],
            )
            return None

    def _get_check_status(
        self, gh_cmd: str, pr_number: int
    ) -> Optional[Tuple[bool, List[str], List[str]]]:
        """Get check status for a PR.

        Returns ``(all_passing, failing_check_names, pending_check_names)``
        or None on failure.
        """
        try:
            cmd = (
                f"{gh_cmd} pr checks {pr_number} --json name,bucket"
            )
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception(
                "pr-monitor: exception checking status for PR #%d", pr_number
            )
            return None

        # gh pr checks returns exit code 1 when checks are failing, so parse
        # stdout regardless of returncode.
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # If no stdout but successful return code, treat as no checks configured
        if not stdout and proc.returncode == 0:
            return (True, [], [])

        # Try to parse JSON; if that fails, see if gh printed a human message
        # like "no checks reported on the '<branch>' branch" and treat that
        # as no checks configured.
        try:
            checks = json.loads(stdout)
        except Exception:
            combined = (stdout + "\n" + stderr).lower()
            if "no checks reported" in combined or "no checks found" in combined:
                LOG.info(
                    "pr-monitor: no checks configured for PR #%d (gh message)",
                    pr_number,
                )
                return (True, [], [])
            LOG.warning(
                "pr-monitor: invalid JSON from gh pr checks for PR #%d: %r",
                pr_number,
                stdout[:512],
            )
            return None

        if not isinstance(checks, list):
            return None

        failing: List[str] = []
        pending: List[str] = []

        for check in checks:
            name = check.get("name", "(unknown)")

            # Use the documented `bucket` field exclusively. If `bucket` is
            # missing that indicates we cannot reliably interpret the check
            # status in this environment — treat as a retrieval failure so
            # the caller can decide (we return None).  This removes legacy
            # fallbacks that attempted to interpret older `state` fields.
            bucket = check.get("bucket")
            if bucket is None:
                LOG.warning(
                    "pr-monitor: check object missing 'bucket' for %s on PR #%d",
                    name,
                    pr_number,
                )
                return None

            bucket = str(bucket).lower()

            # bucket values documented: pass, fail, pending, skipping and cancel
            if bucket in ("pass", "skipping"):
                # pass / skipping -> treat as passing
                continue
            if bucket == "pending":
                pending.append(name)
                continue
            if bucket in ("fail", "cancel"):
                failing.append(name)
                continue

            # Unknown bucket value — log and skip
            LOG.debug(
                "pr-monitor: unknown check bucket=%r for %s on PR #%d",
                bucket,
                name,
                pr_number,
            )

        all_passing = len(failing) == 0 and len(pending) == 0
        return (all_passing, failing, pending)

    def _has_existing_comment(
        self, gh_cmd: str, pr_number: int, marker: str
    ) -> bool:
        """Check whether a comment with the given marker already exists on the PR."""
        try:
            cmd = f"{gh_cmd} pr view {pr_number} --json comments"
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                return False
            stdout = (proc.stdout or "").strip()
            if not stdout:
                return False
            data = json.loads(stdout)
            comments = data.get("comments", [])
            for c in comments:
                body = c.get("body", "")
                if marker in body:
                    return True
            return False
        except Exception:
            LOG.exception(
                "pr-monitor: error checking existing comments on PR #%d",
                pr_number,
            )
            return False

    def _handle_ready_pr(
        self,
        gh_cmd: str,
        pr_number: int,
        pr_title: str,
        pr_url: str,
    ) -> None:
        """Post 'ready for review' comments on GitHub and Worklog."""
        LOG.info("pr-monitor: PR #%d (%s) — all checks passing", pr_number, pr_title)

        # Post GitHub PR comment
        comment_body = (
            f"{_READY_COMMENT_MARKER}\n"
            f"## All CI checks are passing\n\n"
            f"This PR is **ready for review**.\n\n"
            f"_Posted automatically by AMPA PR Monitor._"
        )
        self._post_gh_comment(gh_cmd, pr_number, comment_body)

        # Post Worklog comment (on any work item linked to this PR branch)
        wl_comment = (
            f"PR #{pr_number} ({pr_title}) — all CI checks passing, "
            f"ready for review. URL: {pr_url}"
        )
        self._post_wl_comment(pr_number, pr_title, wl_comment)

        # Send Discord notification using an embed payload when possible.
        try:
            if self._notifier is not None:
                # Build a minimal content fallback plus an embed for rich display.
                payload = {
                    "content": f"PR #{pr_number} ready for review: {pr_title} {pr_url}",
                    "embeds": [
                        {
                            "title": f"PR #{pr_number} ready for review",
                            "description": f"**{pr_title}**\nAll required checks are passing.",
                            "url": pr_url,
                            # Soft green
                            "color": 0x2ecc71,
                        }
                    ],
                }
                self._send_notification(payload=payload, message_type="command")
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send ready notification for PR #%d",
                pr_number,
            )

    # ------------------------------------------------------------------
    # Auto-review dispatch
    # ------------------------------------------------------------------

    def _dispatch_review(
        self,
        gh_cmd: str,
        pr: Dict[str, Any],
        pr_number: int,
        pr_title: str,
        force: bool = False,
    ) -> None:
        """Dispatch an LLM audit session for a CI-passing PR.

        Skips silently when:
        - No dispatcher is configured.
        - No work-item ID can be extracted from the PR.
        - A dispatch marker already exists for this PR number.

        Failures are logged and recorded as work-item comments but never
        propagate — the rest of the pr-monitor run continues unaffected.
        """
        if self._dispatcher is None:
            LOG.debug(
                "pr-monitor: auto_review enabled but no dispatcher configured — "
                "skipping PR #%d",
                pr_number,
            )
            return

        # Extract work-item ID
        work_item_id = self._extract_work_item_id(pr, gh_cmd)
        if not work_item_id:
            LOG.debug(
                "pr-monitor: no work-item ID found for PR #%d — "
                "skipping auto-review",
                pr_number,
            )
            return

        # Check for existing dispatch
        if not force:
            existing = self._get_audit_dispatch_state(work_item_id, pr_number)
            if existing is not None:
                LOG.info(
                    "pr-monitor: audit already dispatched for PR #%d "
                    "(work item %s) — skipping",
                    pr_number,
                    work_item_id,
                )
                return

        # Compose review prompt
        prompt = (
            f"/implement {work_item_id} "
            f"Review PR #{pr_number} ({pr_title}) against the acceptance "
            f"criteria of work item {work_item_id}. "
            f"Post your audit results as a structured comment on the work "
            f"item using the marker format: "
            f"{_AUDIT_RESULT_MARKER}"
        )
        command = f'opencode run "{prompt}"'

        # Dispatch
        try:
            result = self._dispatcher.dispatch(
                command=command, work_item_id=work_item_id
            )
        except Exception:
            LOG.exception(
                "pr-monitor: dispatch failed for PR #%d (work item %s)",
                pr_number,
                work_item_id,
            )
            return

        if result.success:
            LOG.info(
                "pr-monitor: dispatched audit for PR #%d → work item %s "
                "(pid=%s, container=%s)",
                pr_number,
                work_item_id,
                result.pid,
                result.container_id,
            )
            self._post_audit_dispatch_marker(
                work_item_id,
                pr_number,
                dispatched_at=result.timestamp.isoformat(),
                container_id=result.container_id or "",
            )
            self._metric_inc("llm_reviews_dispatched")
        else:
            LOG.warning(
                "pr-monitor: audit dispatch failed for PR #%d "
                "(work item %s): %s",
                pr_number,
                work_item_id,
                result.error,
            )
            # Record the failure as a comment so operators can see it
            try:
                fail_comment = (
                    f"Auto-review dispatch failed for PR #{pr_number}: "
                    f"{result.error}"
                )
                self._wl_shell(
                    f"wl comment add {work_item_id} "
                    f'--comment "{fail_comment}" '
                    f'--author "pr-monitor"',
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                )
            except Exception:
                LOG.debug(
                    "pr-monitor: failed to post dispatch failure comment "
                    "for work item %s",
                    work_item_id,
                )

    # ------------------------------------------------------------------
    # Audit result collection and Discord presentation
    # ------------------------------------------------------------------

    def _check_and_present_audit_results(
        self,
        gh_cmd: str,
        pr: Dict[str, Any],
        pr_number: int,
        pr_title: str,
        pr_url: str,
    ) -> None:
        """Check for completed audit results and present them in Discord.

        Called on subsequent runs for PRs that are already marked ready
        and have a dispatch in progress.  If the audit agent has posted
        results, a Discord embed with Approve/Reject buttons is sent.

        If no dispatch marker or no results yet, this is a no-op.
        """
        try:
            work_item_id = self._extract_work_item_id(pr, gh_cmd)
            if not work_item_id:
                return

            # Check if a dispatch was made
            dispatch_state = self._get_audit_dispatch_state(
                work_item_id, pr_number
            )
            if dispatch_state is None:
                # No dispatch recorded — nothing to check.
                # (This can happen if auto_review was just enabled and the
                # PR was already marked ready before dispatch existed.)
                # Trigger a dispatch instead.
                self._dispatch_review(gh_cmd, pr, pr_number, pr_title)
                return

            # Check for audit results
            pr_updated_at = str(pr.get("updatedAt") or "").strip() or None
            audit = self._get_audit_result(
                work_item_id, pr_number, after_iso=pr_updated_at
            )

            # If we found an audit result but it is stale (older than the PR
            # update time), trigger a fresh dispatch.
            if audit is None and pr_updated_at:
                maybe_stale = self._get_audit_result(work_item_id, pr_number)
                if maybe_stale is not None:
                    LOG.info(
                        "pr-monitor: stale audit result for PR #%d "
                        "(work item %s) — re-dispatching",
                        pr_number,
                        work_item_id,
                    )
                    self._dispatch_review(
                        gh_cmd, pr, pr_number, pr_title, force=True
                    )
                    return

            if audit is None:
                LOG.debug(
                    "pr-monitor: no audit result yet for PR #%d "
                    "(work item %s) — audit may still be running",
                    pr_number,
                    work_item_id,
                )
                return

            # Present the results in Discord
            if self._review_action_taken(pr_number):
                LOG.info(
                    "pr-monitor: review decision already recorded for PR #%d — "
                    "not re-presenting audit",
                    pr_number,
                )
                return
            self._present_audit_results(
                pr_number, pr_title, pr_url, work_item_id, audit
            )
        except Exception:
            LOG.exception(
                "pr-monitor: error checking audit results for PR #%d",
                pr_number,
            )

    def _present_audit_results(
        self,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        work_item_id: str,
        audit: Dict[str, Any],
    ) -> None:
        """Build and send a Discord embed with audit results and action buttons.

        The embed includes:
        - Overall verdict (pass/fail/partial)
        - Per-criterion results
        - Concerns
        - Approve Merge / Reject buttons

        Button custom_ids follow the convention:
        ``pr_review_approve_{pr_number}`` and ``pr_review_reject_{pr_number}``.
        """
        if self._notifier is None:
            LOG.debug(
                "pr-monitor: no notifier — cannot present audit results "
                "for PR #%d",
                pr_number,
            )
            return

        overall = audit.get("overall", "unknown")
        summary = audit.get("summary", "No summary available.")
        concerns = audit.get("concerns", [])
        criteria = audit.get("criteria", [])

        # Colour: green=pass, red=fail, yellow=partial
        colour_map = {"pass": 0x2ECC71, "fail": 0xE74C3C, "partial": 0xF39C12}
        colour = colour_map.get(overall, 0x95A5A6)

        # Build criteria fields
        fields = []
        for c in criteria[:10]:  # Cap at 10 to avoid embed limits
            name = c.get("name", "Criterion")
            passed = c.get("pass", False)
            notes = c.get("notes", "")
            icon = "PASS" if passed else "FAIL"
            value = f"[{icon}] {notes}" if notes else f"[{icon}]"
            fields.append({"name": name, "value": value, "inline": False})

        if concerns:
            concern_text = "\n".join(f"- {c}" for c in concerns[:5])
            fields.append({
                "name": "Concerns",
                "value": concern_text,
                "inline": False,
            })

        embed = {
            "title": f"Audit: PR #{pr_number} — {overall.upper()}",
            "description": f"**{pr_title}**\n\n{summary}",
            "url": pr_url,
            "color": colour,
            "fields": fields,
            "footer": {"text": f"Work item: {work_item_id}"},
        }

        components = [
            {
                "type": "button",
                "label": "Approve Merge",
                "style": "success",
                "custom_id": f"pr_review_approve_{pr_number}",
            },
            {
                "type": "button",
                "label": "Reject",
                "style": "danger",
                "custom_id": f"pr_review_reject_{pr_number}",
            },
        ]

        payload = {
            "content": (
                f"Audit complete for PR #{pr_number}: "
                f"{pr_title} — {overall.upper()}"
            ),
            "embeds": [embed],
            "components": components,
        }

        session_id = f"pr-review-{pr_number}"
        try:
            from . import conversation_manager

            prompt = (
                f"Review decision required for PR #{pr_number} ({pr_title}). "
                "Choose accept to merge and close the work item, or decline "
                "to reject without merging."
            )
            conversation_manager.start_conversation(
                session_id,
                prompt,
                {
                    "work_item": work_item_id,
                    "summary": (
                        f"PR review decision required for PR #{pr_number} "
                        f"({overall.upper()})"
                    ),
                    "choices": ["accept", "decline"],
                    "context": [
                        {
                            "pr_number": pr_number,
                            "work_item_id": work_item_id,
                            "pr_url": pr_url,
                            "audit_overall": overall,
                        }
                    ],
                },
            )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to start review conversation for PR #%d",
                pr_number,
            )

        try:
            sent = self._send_notification(payload=payload, message_type="command")
            if sent:
                self._metric_inc("llm_reviews_presented")
            LOG.info(
                "pr-monitor: presented audit results for PR #%d "
                "(verdict: %s)",
                pr_number,
                overall,
            )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send audit results for PR #%d",
                pr_number,
            )

    def _handle_failing_pr(
        self,
        gh_cmd: str,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        failing_checks: List[str],
    ) -> None:
        """Create critical work item and post comments for failing PR."""
        LOG.warning(
            "pr-monitor: PR #%d (%s) — %d check(s) failing: %s",
            pr_number,
            pr_title,
            len(failing_checks),
            ", ".join(failing_checks),
        )

        checks_str = ", ".join(failing_checks)

        # Create a critical Worklog work item
        wl_title = f"CI failing on PR #{pr_number}: {pr_title}"
        wl_desc = (
            f"The following required checks are failing on PR #{pr_number} "
            f"({pr_title}):\n\n"
            f"- {chr(10).join('- ' + c for c in failing_checks) if len(failing_checks) > 1 else failing_checks[0]}\n\n"
            f"PR URL: {pr_url}\n\n"
            f"discovered-from:SA-0MMJY1K3W15RI0F4\n\n"
            f"_Created automatically by AMPA PR Monitor._"
        )
        self._create_critical_work_item(wl_title, wl_desc)

        # Post GitHub PR comment about failure
        comment_body = (
            f"{_FAILURE_COMMENT_MARKER}\n"
            f"## CI checks are failing\n\n"
            f"The following required checks are failing:\n"
            f"{''.join('- ' + c + chr(10) for c in failing_checks)}\n"
            f"A critical work item has been created to track this.\n\n"
            f"_Posted automatically by AMPA PR Monitor._"
        )
        self._post_gh_comment(gh_cmd, pr_number, comment_body)

        # Send Discord notification
        try:
            if self._notifier is not None:
                # Build an embed containing the failing checks for richer display.
                fields = []
                if failing_checks:
                    # Put up to 10 failing checks into a single field; others are joined.
                    fields.append({
                        "name": "Failing checks",
                        "value": "\n".join(failing_checks[:10]),
                        "inline": False,
                    })

                payload = {
                    "content": f"CI failing on PR #{pr_number}: {pr_title} {pr_url}",
                    "embeds": [
                        {
                            "title": f"CI failing on PR #{pr_number}",
                            "description": f"**{pr_title}**\n{pr_url}",
                            "color": 0xe74c3c,
                            "fields": fields,
                        }
                    ],
                }
                self._send_notification(payload=payload, message_type="error")
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send failure notification for PR #%d",
                pr_number,
            )

    def _post_gh_comment(
        self, gh_cmd: str, pr_number: int, body: str
    ) -> bool:
        """Post a comment on a GitHub PR.  Returns True on success."""
        try:
            proc = self.run_shell(
                [gh_cmd, "pr", "comment", str(pr_number), "--body", body],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: gh pr comment failed for PR #%d: rc=%s stderr=%r",
                    pr_number,
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return False
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception posting GH comment on PR #%d",
                pr_number,
            )
            return False

    def _post_wl_comment(
        self, pr_number: int, pr_title: str, comment: str
    ) -> None:
        """Post a Worklog comment.  Best-effort — failures are logged."""
        try:
            # Search for work items that reference this PR
            proc = self._wl_shell(
                f"wl search 'PR #{pr_number}' --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                try:
                    items = json.loads(proc.stdout.strip())
                    if isinstance(items, list) and items:
                        wid = items[0].get("id")
                        if wid:
                            self._wl_shell(
                                f'wl comment add {wid} --comment "{comment}" '
                                f'--author "ampa-pr-monitor" --json',
                                shell=True,
                                check=False,
                                capture_output=True,
                                text=True,
                                cwd=self.command_cwd,
                            )
                except Exception:
                    LOG.exception(
                        "pr-monitor: failed to parse wl search results for PR #%d",
                        pr_number,
                    )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to post WL comment for PR #%d", pr_number
            )

    def _create_critical_work_item(self, title: str, description: str) -> Optional[str]:
        """Create a critical Worklog work item.  Returns the new item id or None."""
        try:
            proc = self._wl_shell(
                [
                    "wl", "create",
                    "--title", title,
                    "--description", description,
                    "--priority", "critical",
                    "--issue-type", "bug",
                    "--json",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: wl create failed: rc=%s stderr=%r",
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return None
            stdout = (proc.stdout or "").strip()
            if stdout:
                try:
                    data = json.loads(stdout)
                    return data.get("id") or data.get("workItem", {}).get("id")
                except Exception:
                    pass
            return None
        except Exception:
            LOG.exception("pr-monitor: exception creating critical work item")
            return None

    # ------------------------------------------------------------------
    # Work-item ID extraction
    # ------------------------------------------------------------------

    def _extract_work_item_id(
        self, pr: Dict[str, Any], gh_cmd: str
    ) -> Optional[str]:
        """Extract a work-item ID from a PR.

        Checks, in order:
        1. The branch name (``headRefName``) for patterns like
           ``feature/<ID>-*``, ``bug/<ID>-*``, ``wl-<ID>-*``.
        2. The PR body for markers like ``work-item: <ID>``,
           ``closes <ID>``, ``fixes <ID>``.

        Returns the work-item ID string or ``None`` if not found.
        """
        # 1. Try branch name
        branch = pr.get("headRefName", "")
        if branch:
            m = _WORK_ITEM_ID_BRANCH_RE.search(branch)
            if m:
                candidate = m.group(1)
                # Validate it looks like a work-item ID (has a prefix separator)
                if "-" in candidate:
                    return candidate

        # 2. Try PR body
        pr_number = pr.get("number")
        if pr_number is not None:
            try:
                cmd = f"{gh_cmd} pr view {pr_number} --json body"
                proc = self.run_shell(
                    cmd,
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                )
                if proc.returncode == 0 and (proc.stdout or "").strip():
                    data = json.loads(proc.stdout.strip())
                    body = data.get("body", "")
                    m = _WORK_ITEM_ID_BODY_RE.search(body)
                    if m:
                        return m.group(1)
            except Exception:
                LOG.debug(
                    "pr-monitor: exception extracting work item from PR #%s body",
                    pr_number,
                )

        return None

    # ------------------------------------------------------------------
    # Audit dispatch state tracking
    # ------------------------------------------------------------------

    def _get_audit_dispatch_state(
        self, work_item_id: str, pr_number: int
    ) -> Optional[Dict[str, Any]]:
        """Query a work item's comments for a dispatch marker for *pr_number*.

        Returns the parsed dispatch state dict or ``None`` if not found.
        """
        marker = f"{_AUDIT_DISPATCH_MARKER_PREFIX}{pr_number} -->"
        try:
            proc = self._wl_shell(
                f"wl show {work_item_id} --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                return None
            stdout = (proc.stdout or "").strip()
            if not stdout:
                return None
            data = json.loads(stdout)
            comments = data.get("comments", [])
            if not comments and "workItem" in data:
                comments = data.get("comments", [])
            for comment in comments:
                body = comment.get("comment", "")
                if marker in body:
                    return self._parse_marker_json(body, marker)
        except Exception:
            LOG.debug(
                "pr-monitor: exception reading dispatch state for %s PR #%d",
                work_item_id,
                pr_number,
            )
        return None

    def _get_audit_result(
        self,
        work_item_id: str,
        pr_number: int,
        after_iso: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Query a work item's comments for an audit result for *pr_number*.

        If *after_iso* is provided, only results whose ``audited_at``
        timestamp is after that ISO-8601 string are considered.

        Returns the parsed audit result dict or ``None``.
        """
        try:
            proc = self._wl_shell(
                f"wl show {work_item_id} --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                return None
            stdout = (proc.stdout or "").strip()
            if not stdout:
                return None
            data = json.loads(stdout)
            comments = data.get("comments", [])
            for comment in comments:
                body = comment.get("comment", "")
                if _AUDIT_RESULT_MARKER not in body:
                    continue
                result = self._parse_marker_json(body, _AUDIT_RESULT_MARKER)
                if result is None:
                    continue
                audit = result.get("audit_result", result)
                # Check PR number match if present
                if audit.get("pr_number") is not None and audit.get("pr_number") != pr_number:
                    continue
                # Check freshness
                if after_iso and audit.get("audited_at"):
                    if audit["audited_at"] <= after_iso:
                        LOG.debug(
                            "pr-monitor: stale audit result for %s PR #%d "
                            "(audited_at=%s <= %s)",
                            work_item_id,
                            pr_number,
                            audit["audited_at"],
                            after_iso,
                        )
                        continue
                return audit
        except Exception:
            LOG.debug(
                "pr-monitor: exception reading audit result for %s PR #%d",
                work_item_id,
                pr_number,
            )
        return None

    def _post_audit_dispatch_marker(
        self,
        work_item_id: str,
        pr_number: int,
        dispatched_at: str,
        container_id: Optional[str] = None,
    ) -> bool:
        """Post a dispatch state marker comment to a work item.

        Returns True on success.
        """
        marker = f"{_AUDIT_DISPATCH_MARKER_PREFIX}{pr_number} -->"
        payload = json.dumps({
            "dispatch_state": {
                "pr_number": pr_number,
                "dispatched_at": dispatched_at,
                "container_id": container_id,
                "work_item_id": work_item_id,
            }
        })
        comment_body = f"{marker}\n{payload}"
        try:
            proc = self._wl_shell(
                [
                    "wl", "comment", "add", work_item_id,
                    "--comment", comment_body,
                    "--author", "ampa-pr-monitor",
                    "--json",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: failed to post dispatch marker for %s PR #%d: %s",
                    work_item_id,
                    pr_number,
                    (proc.stderr or "")[:256],
                )
                return False
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception posting dispatch marker for %s PR #%d",
                work_item_id,
                pr_number,
            )
            return False

    @staticmethod
    def _parse_marker_json(
        body: str, marker: str
    ) -> Optional[Dict[str, Any]]:
        """Extract the JSON object following *marker* in a comment body."""
        idx = body.find(marker)
        if idx < 0:
            return None
        rest = body[idx + len(marker) :].strip()
        # Try to find a JSON object in the remaining text
        brace = rest.find("{")
        if brace < 0:
            return None
        # Find the matching closing brace
        depth = 0
        for i, ch in enumerate(rest[brace:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(rest[brace : brace + i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    # ------------------------------------------------------------------
    # Phase 4 — Merge / reject / cleanup on operator decision
    # ------------------------------------------------------------------

    def handle_review_decision(
        self,
        action: str,
        pr_number: int,
        work_item_id: Optional[str] = None,
        approved_by: str = "unknown",
        gh_cmd: str = _DEFAULT_GH_COMMAND,
    ) -> Dict[str, Any]:
        """Process an operator's approve or reject decision for a PR review.

        This is the main entry point called after the Discord interaction is
        routed through the conversation manager.  It orchestrates merge (or
        rejection), work-item closure, branch cleanup, and notifications.

        Parameters
        ----------
        action
            ``"accept"`` to merge, anything else to reject.
        pr_number
            The GitHub PR number.
        work_item_id
            Optional work-item ID associated with the PR.  If *None*, the
            method will attempt to extract it from the PR.
        approved_by
            Human-readable identifier of the operator (e.g. Discord username).
        gh_cmd
            The ``gh`` CLI executable name / path.

        Returns
        -------
        dict
            ``action`` key is ``"merged"``, ``"rejected"``, or ``"error"``
            with a ``note`` describing the outcome.
        """
        is_approve = str(action).strip().lower() in ("accept", "approve")
        LOG.info(
            "pr-monitor: handling review decision action=%s pr_number=%d "
            "work_item_id=%s approved_by=%s",
            action,
            pr_number,
            work_item_id,
            approved_by,
        )

        if is_approve:
            return self._handle_approve(
                gh_cmd, pr_number, work_item_id, approved_by
            )
        return self._handle_reject(
            gh_cmd, pr_number, work_item_id, approved_by
        )

    def _handle_approve(
        self,
        gh_cmd: str,
        pr_number: int,
        work_item_id: Optional[str],
        approved_by: str,
    ) -> Dict[str, Any]:
        """Execute merge, work-item closure, branch cleanup, and notifications."""
        # 1. Merge the PR
        merge_ok, merge_note = self._merge_pr(gh_cmd, pr_number)
        if not merge_ok:
            self._notify_review_outcome(
                pr_number,
                "Merge Failed",
                merge_note,
                color=0xE74C3C,
            )
            return {"action": "error", "note": merge_note}

        # 2. Close the associated work item (best-effort)
        if work_item_id:
            reason = (
                f"PR #{pr_number} merged via auto-review approval "
                f"by {approved_by}"
            )
            self._close_work_item(work_item_id, reason)

        # 3. Delete the remote branch (best-effort)
        branch = self._get_pr_branch(gh_cmd, pr_number)
        if branch:
            self._cleanup_branch(gh_cmd, branch)

        # 4. Record a Worklog comment (best-effort)
        if work_item_id:
            comment = (
                f"PR #{pr_number} merged and branch cleaned up. "
                f"Approved by {approved_by} via Discord."
            )
            self._add_wl_comment(work_item_id, comment)

        # 5. Send confirmation notification to Discord
        self._notify_review_outcome(
            pr_number,
            "PR Merged",
            f"PR #{pr_number} has been merged and cleaned up.\n"
            f"Approved by **{approved_by}**.",
            color=0x2ECC71,
        )

        return {
            "action": "merged",
            "note": f"PR #{pr_number} merged by {approved_by}",
        }

    def _handle_reject(
        self,
        gh_cmd: str,
        pr_number: int,
        work_item_id: Optional[str],
        approved_by: str,
    ) -> Dict[str, Any]:
        """Post rejection comment on PR and record in Worklog."""
        # 1. Post a rejection comment on the PR
        body = (
            f"This PR was **declined** during auto-review by **{approved_by}**.\n\n"
            "The automated audit was reviewed and the operator chose not to "
            "merge at this time.  Please address any concerns and re-request "
            "review when ready."
        )
        self._post_gh_comment(gh_cmd, pr_number, body)

        # 2. Record rejection in Worklog (best-effort)
        if work_item_id:
            comment = (
                f"PR #{pr_number} review rejected by {approved_by} "
                "via Discord.  PR comment posted."
            )
            self._add_wl_comment(work_item_id, comment)

        # 3. Notify Discord
        self._notify_review_outcome(
            pr_number,
            "PR Review Rejected",
            f"PR #{pr_number} was declined by **{approved_by}**.\n"
            "A comment has been posted on the PR.",
            color=0xE74C3C,
        )

        return {
            "action": "rejected",
            "note": f"PR #{pr_number} rejected by {approved_by}",
        }

    def _merge_pr(
        self, gh_cmd: str, pr_number: int
    ) -> Tuple[bool, str]:
        """Merge a PR via ``gh pr merge``.

        Returns ``(success, note)`` where *note* describes the outcome.
        """
        try:
            proc = self.run_shell(
                [gh_cmd, "pr", "merge", str(pr_number), "--merge"],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()[:512]
                note = (
                    f"pr-monitor: gh pr merge failed for PR #{pr_number}: "
                    f"rc={proc.returncode} stderr={stderr!r}"
                )
                LOG.error(note)
                return False, note
            LOG.info("pr-monitor: merged PR #%d", pr_number)
            return True, f"PR #{pr_number} merged successfully"
        except Exception as exc:
            note = f"pr-monitor: exception merging PR #{pr_number}: {exc}"
            LOG.exception(note)
            return False, note

    def _reject_pr(
        self, gh_cmd: str, pr_number: int, reason: str
    ) -> bool:
        """Post a rejection comment on a PR.  Returns True on success."""
        return self._post_gh_comment(gh_cmd, pr_number, reason)

    def _close_work_item(self, work_item_id: str, reason: str) -> bool:
        """Close a Worklog work item.  Best-effort — failures are logged."""
        try:
            proc = self._wl_shell(
                [
                    "wl", "close", work_item_id,
                    "--reason", reason,
                    "--json",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: wl close failed for %s: rc=%s stderr=%r",
                    work_item_id,
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return False
            LOG.info("pr-monitor: closed work item %s", work_item_id)
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception closing work item %s", work_item_id
            )
            return False

    def _cleanup_branch(self, gh_cmd: str, branch: str) -> bool:
        """Delete a remote branch.  Best-effort — failures are logged."""
        try:
            proc = self.run_shell(
                ["git", "push", "origin", "--delete", branch],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: branch cleanup failed for %s: rc=%s stderr=%r",
                    branch,
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return False
            LOG.info("pr-monitor: deleted remote branch %s", branch)
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception deleting branch %s", branch
            )
            return False

    def _get_pr_branch(
        self, gh_cmd: str, pr_number: int
    ) -> Optional[str]:
        """Retrieve the head branch name for a PR.  Returns None on failure."""
        try:
            proc = self.run_shell(
                f"{gh_cmd} pr view {pr_number} --json headRefName -q .headRefName",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                return proc.stdout.strip()
            return None
        except Exception:
            LOG.exception(
                "pr-monitor: failed to get branch for PR #%d", pr_number
            )
            return None

    def _add_wl_comment(self, work_item_id: str, comment: str) -> bool:
        """Add a comment to a Worklog work item.  Best-effort."""
        try:
            proc = self._wl_shell(
                [
                    "wl", "comment", "add", work_item_id,
                    "--comment", comment,
                    "--author", "ampa-pr-monitor",
                    "--json",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: wl comment add failed for %s: rc=%s",
                    work_item_id,
                    proc.returncode,
                )
                return False
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception adding WL comment to %s", work_item_id
            )
            return False

    def _notify_review_outcome(
        self,
        pr_number: int,
        title: str,
        description: str,
        color: int = 0x3498DB,
    ) -> None:
        """Send a Discord notification about a review outcome."""
        if not self._notifier:
            return
        try:
            payload = {
                "content": f"{title} — PR #{pr_number}",
                "embeds": [
                    {
                        "title": title,
                        "description": description,
                        "color": color,
                    }
                ],
            }
            self._send_notification(payload=payload, message_type="command")
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send review outcome notification "
                "for PR #%d",
                pr_number,
            )

    def _review_action_taken(self, pr_number: int) -> bool:
        """Return True when a PR review session has already been resumed."""
        session_id = f"pr-review-{pr_number}"
        tool_output_dir = os.getenv("AMPA_TOOL_OUTPUT_DIR") or os.path.join(
            tempfile.gettempdir(), "opencode_tool_output"
        )
        state_path = os.path.join(tool_output_dir, f"session_{session_id}.json")
        if not os.path.exists(state_path):
            return False
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state_data = json.load(fh)
            return str(state_data.get("state", "")).strip().lower() == "running"
        except Exception:
            LOG.debug(
                "pr-monitor: failed to read review session state for PR #%d",
                pr_number,
            )
            return False

    def _notify_summary(
        self,
        ready_prs: List[int],
        failing_prs: List[int],
        skipped_prs: List[int],
        total: int,
        prs: List[Dict[str, Any]],
    ) -> None:
        """Send a Discord summary notification for the entire run."""
        if not self._notifier:
            return
        try:
            # Build a mapping from PR number to title/url for link formatting
            pr_map: Dict[int, Dict[str, str]] = {}
            for p in prs:
                num_raw = p.get("number")
                try:
                    num = int(str(num_raw))
                except Exception:
                    continue
                pr_map[num] = {"title": p.get("title", f"PR #{num}"), "url": p.get("url", "")}

            lines = [f"Checked **{total}** open PR(s)."]
            if ready_prs:
                ready_links = []
                for n in ready_prs:
                    meta = pr_map.get(n)
                    if meta and meta.get("url"):
                        ready_links.append(f"[{meta.get('title')}]({meta.get('url')})")
                    else:
                        ready_links.append(f"#{n}")
                lines.append(f"Ready for review: {', '.join(ready_links)}")
            if failing_prs:
                fail_links = []
                for n in failing_prs:
                    meta = pr_map.get(n)
                    if meta and meta.get("url"):
                        fail_links.append(f"[{meta.get('title')}]({meta.get('url')})")
                    else:
                        fail_links.append(f"#{n}")
                lines.append(f"CI failing: {', '.join(fail_links)}")
            if skipped_prs:
                skip_links = []
                for n in skipped_prs:
                    meta = pr_map.get(n)
                    if meta and meta.get("url"):
                        skip_links.append(f"[{meta.get('title')}]({meta.get('url')})")
                    else:
                        skip_links.append(f"#{n}")
                lines.append(
                    f"Skipped (already notified or pending): {', '.join(skip_links)}"
                )
            # Build an embed summary so the message appears nicely in Discord.
            summary_description = "\n".join(lines)
            payload = {
                "content": f"PR Monitor Summary — checked {total} PR(s)",
                "embeds": [
                    {
                        "title": "PR Monitor Summary",
                        "description": summary_description,
                        "color": 0x3498db,
                    }
                ],
            }
            self._send_notification(payload=payload, message_type="command")
        except Exception:
            LOG.exception("pr-monitor: failed to send summary notification")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    """Coerce a metadata value to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
