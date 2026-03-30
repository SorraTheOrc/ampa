from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ampa.engine.core import Engine, EngineConfig, EngineResult, EngineStatus
from ampa.engine.descriptor import (
    WorkflowDescriptor,
    Metadata,
    Role,
    Invariant,
    Command,
    Effects,
    Notification,
    StateTuple,
)
from ampa.engine.invariants import (
    InvariantEvaluator,
    InvariantResult,
    SingleInvariantResult,
)
from ampa.engine.dispatch import DispatchResult, Dispatcher


# Simple test helpers (local copies of the mock objects used elsewhere)
class SimpleFetcher:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        return self._data


class SimpleUpdater:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "status": status,
                "stage": stage,
                "assignee": assignee,
            }
        )
        return True


class SimpleCommentWriter:
    def __init__(self):
        self.comments: list[dict[str, str]] = []

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        self.comments.append(
            {"work_item_id": work_item_id, "comment": comment, "author": author}
        )
        return True


class SimpleNotifier:
    def __init__(self):
        self.messages: list[dict[str, str]] = []

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        self.messages.append({"message": message, "title": title, "level": level})
        return True


class SimpleRecorder:
    def __init__(self):
        self.records: list[dict[str, Any]] = []

    def record_dispatch(self, record: dict[str, Any]) -> str | None:
        self.records.append(record)
        return f"DR-{len(self.records)}"


class SimpleDispatcher(Dispatcher):
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, *, command: str = "", work_item_id: str = "") -> DispatchResult:
        # Minimal DispatchResult-like object
        class R:
            def __init__(self):
                self.success = True
                self.pid = 12345
                self.error = None
                self.timestamp = datetime.now(timezone.utc)

        self.calls.append({"command": command, "work_item_id": work_item_id})
        return R()


def _make_descriptor() -> WorkflowDescriptor:
    # Minimal descriptor with delegate command and requires_work_item_context invariant
    inv = Invariant(
        name="requires_work_item_context",
        description="PM quality gate",
        when=("pre",),
        logic="length(description) > 100",
    )

    delegate_cmd = Command(
        name="delegate",
        description="Delegate work",
        from_states=("plan",),
        to="delegated",
        actor="PM",
        pre=("requires_work_item_context",),
        dispatch_map={"plan": 'echo "implement {id}"'},
        effects=Effects(
            set_assignee="Patch",
            add_tags=("delegated",),
            notifications=(
                Notification(
                    channel="discord", message="Delegating task for ${title} (${id})"
                ),
            ),
        ),
    )

    return WorkflowDescriptor(
        version="1.0.0",
        metadata=Metadata(
            name="test", description="test", owner="test", roles=(Role(name="PM"),)
        ),
        statuses=("open", "in_progress", "in-progress", "completed"),
        stages=("plan_complete", "in_progress", "in_review"),
        states={
            "plan": StateTuple(status="open", stage="plan_complete"),
            "delegated": StateTuple(status="in-progress", stage="in_progress"),
        },
        terminal_states=("shipped",),
        invariants=(inv,),
        commands={"delegate": delegate_cmd},
    )


class AuditMockEvaluator:
    """Evaluator wrapper that delegates to a base evaluator but treats
    'requires_work_item_context' by calling an injected audit function.

    The audit_fn(work_item) -> bool emulates the audit skill verdict.
    """

    def __init__(
        self, base: InvariantEvaluator, audit_fn: Callable[[dict[str, Any]], bool]
    ):
        self._base = base
        self._audit_fn = audit_fn

    def evaluate(self, names, work_item, *, fail_fast: bool = True):
        results = []
        all_passed = True
        for name in names:
            if name == "requires_work_item_context":
                passed = bool(self._audit_fn(work_item))
                reason = (
                    "audit: insufficient context"
                    if not passed
                    else "audit: sufficient context"
                )
                results.append(
                    SingleInvariantResult(name=name, passed=passed, reason=reason)
                )
                if not passed:
                    all_passed = False
                    if fail_fast:
                        break
            else:
                # Delegate to base evaluator for other invariants (evaluate single)
                r = self._base.evaluate((name,), work_item, fail_fast=fail_fast)
                results.extend(r.results)
                if not r.passed:
                    all_passed = False
                    if fail_fast:
                        break

        return InvariantResult(passed=all_passed, results=tuple(results))


def _make_work_item(description: str) -> dict[str, Any]:
    return {
        "workItem": {
            "id": "WL-TEST",
            "title": "Test item",
            "description": description,
            "status": "open",
            "stage": "plan_complete",
            "tags": [],
        },
        "comments": [],
    }


def test_requires_work_item_context_passes_when_audit_ok():
    """When the audit skill says the work item has sufficient context,
    delegation proceeds: state update applied, dispatch occurs, and only
    post-dispatch notification is sent.
    """
    descriptor = _make_descriptor()
    wi = _make_work_item(description=("A" * 200))

    # Base evaluator (not used for the special invariant but required by wrapper)
    base_eval = InvariantEvaluator(descriptor.invariants)

    # Audit function returns True (sufficient context)
    def audit_ok(work_item):
        return True

    evaluator = AuditMockEvaluator(base_eval, audit_ok)

    fetcher = SimpleFetcher(wi)
    updater = SimpleUpdater()
    commenter = SimpleCommentWriter()
    notifier = SimpleNotifier()
    recorder = SimpleRecorder()
    dispatcher = SimpleDispatcher()

    engine = Engine(
        descriptor=descriptor,
        dispatcher=dispatcher,
        candidate_selector=None,  # not used when work_item_id passed
        invariant_evaluator=evaluator,  # type: ignore[arg-type]
        work_item_fetcher=fetcher,
        updater=updater,
        comment_writer=commenter,
        dispatch_recorder=recorder,
        notifier=notifier,
        config=EngineConfig(),
    )

    result = engine.process_delegation(work_item_id="WL-TEST")

    assert result.status == EngineStatus.SUCCESS
    # State transition applied before dispatch
    assert len(updater.calls) == 1
    assert len(dispatcher.calls) == 1
    # Post-dispatch notification only
    assert len(notifier.messages) == 1
    assert notifier.messages[0]["level"] == "info"


def test_requires_work_item_context_blocks_when_audit_fails():
    """When the audit skill reports insufficient context, delegation is
    blocked: no state update, a comment is written, and a warning
    notification is sent.
    """
    descriptor = _make_descriptor()
    wi = _make_work_item(description=("short"))

    base_eval = InvariantEvaluator(descriptor.invariants)

    def audit_fail(work_item):
        return False

    evaluator = AuditMockEvaluator(base_eval, audit_fail)

    fetcher = SimpleFetcher(wi)
    updater = SimpleUpdater()
    commenter = SimpleCommentWriter()
    notifier = SimpleNotifier()
    recorder = SimpleRecorder()
    dispatcher = SimpleDispatcher()

    engine = Engine(
        descriptor=descriptor,
        dispatcher=dispatcher,
        candidate_selector=None,
        invariant_evaluator=evaluator,  # type: ignore[arg-type]
        work_item_fetcher=fetcher,
        updater=updater,
        comment_writer=commenter,
        dispatch_recorder=recorder,
        notifier=notifier,
        config=EngineConfig(),
    )

    result = engine.process_delegation(work_item_id="WL-TEST")

    assert result.status == EngineStatus.INVARIANT_FAILED
    # No state transition applied
    assert len(updater.calls) == 0
    # Comment recorded explaining the blockage
    assert len(commenter.comments) == 1
    assert "blocked" in commenter.comments[0]["comment"].lower()
    # Warning notification sent
    assert len(notifier.messages) == 1
    assert notifier.messages[0]["level"] == "warning"
