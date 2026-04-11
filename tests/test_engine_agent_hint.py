import datetime

from ampa.engine.core import Engine
from ampa.engine.descriptor import (
    WorkflowDescriptor,
    Metadata,
    Command,
    StateTuple,
)
from ampa.engine.dispatch import DryRunDispatcher


class DummyEvaluator:
    def evaluate(self, names, work_item_data, fail_fast=False):
        class R:
            passed = True

        return R()


class StubFetcher:
    def fetch(self, work_item_id: str):
        return {"workItem": {"id": work_item_id, "status": "open", "stage": "plan_complete"}}


def test_engine_injects_agent_into_dispatch_template():
    # Build a minimal workflow descriptor with a delegate command that
    # uses the {agent} placeholder in its dispatch template.
    metadata = Metadata(name="test", description="", owner="", roles=())

    states = {
        "from_alias": StateTuple(status="open", stage="plan_complete"),
        "to_alias": StateTuple(status="in_progress", stage="delegated"),
    }

    delegate_cmd = Command(
        name="delegate",
        description="delegate",
        from_states=("from_alias",),
        to="to_alias",
        actor="agent",
        dispatch_map={"from_alias": 'opencode run "do {id} --agent {agent}"'},
    )

    descriptor = WorkflowDescriptor(
        version="0.1",
        metadata=metadata,
        statuses=("open", "in_progress"),
        stages=("plan_complete", "delegated"),
        states=states,
        terminal_states=(),
        invariants=(),
        commands={"delegate": delegate_cmd},
    )

    dispatcher = DryRunDispatcher()
    engine = Engine(
        descriptor=descriptor,
        dispatcher=dispatcher,
        candidate_selector=None,
        invariant_evaluator=DummyEvaluator(),
        work_item_fetcher=StubFetcher(),
    )

    # Call process_delegation and supply an agent hint. Verify the dispatched
    # command string includes the agent substituted into the template.
    result = engine.process_delegation(work_item_id="WI-1", agent_hint="TestAgent")

    assert result.success
    assert len(dispatcher.calls) == 1
    assert "--agent TestAgent" in dispatcher.calls[0].command
