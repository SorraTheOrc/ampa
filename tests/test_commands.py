"""Tests for descriptor-driven dispatch resolution.

Covers:
- resolve_from_state_alias() on WorkflowDescriptor
- dispatch_map on Command: template lookup and variable substitution
"""

from __future__ import annotations

from ampa.engine.descriptor import (
    Command,
    Metadata,
    Role,
    StateTuple,
    WorkflowDescriptor,
)


def _make_descriptor() -> WorkflowDescriptor:
    """Build a minimal descriptor with a delegate command and dispatch_map."""
    states = {
        "idea": StateTuple(status="open", stage="idea"),
        "intake": StateTuple(status="open", stage="intake_complete"),
        "plan": StateTuple(status="open", stage="plan_complete"),
        "delegated": StateTuple(status="in-progress", stage="delegated"),
    }

    delegate = Command(
        name="delegate",
        description="Delegate work",
        from_states=("idea", "intake", "plan"),
        to="delegated",
        actor="PM",
        dispatch_map={
            "idea": 'opencode run "/intake {id} do not ask questions"',
            "intake": 'opencode run "/plan {id}"',
            "plan": 'opencode run "work on {id} using the implement skill"',
        },
    )

    return WorkflowDescriptor(
        version="1.0.0",
        metadata=Metadata(
            name="test",
            description="Test",
            owner="test",
            roles=(Role(name="PM"),),
        ),
        statuses=("open", "in-progress"),
        stages=("idea", "intake_complete", "plan_complete", "delegated"),
        states=states,
        commands={"delegate": delegate},
    )


# ---------------------------------------------------------------------------
# resolve_from_state_alias tests
# ---------------------------------------------------------------------------


class TestResolveFromStateAlias:
    def test_idea_alias(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        state = StateTuple(status="open", stage="idea")
        assert desc.resolve_from_state_alias(cmd, state) == "idea"

    def test_intake_alias(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        state = StateTuple(status="open", stage="intake_complete")
        assert desc.resolve_from_state_alias(cmd, state) == "intake"

    def test_plan_alias(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        state = StateTuple(status="open", stage="plan_complete")
        assert desc.resolve_from_state_alias(cmd, state) == "plan"

    def test_unknown_state_returns_none(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        state = StateTuple(status="open", stage="in_review")
        assert desc.resolve_from_state_alias(cmd, state) is None

    def test_empty_state_returns_none(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        state = StateTuple(status="", stage="")
        assert desc.resolve_from_state_alias(cmd, state) is None


# ---------------------------------------------------------------------------
# dispatch_map template tests
# ---------------------------------------------------------------------------


class TestDispatchMapTemplates:
    def test_idea_template(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        template = cmd.dispatch_map["idea"]
        result = template.format(id="WL-42")
        assert result == 'opencode run "/intake WL-42 do not ask questions"'

    def test_intake_template(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        template = cmd.dispatch_map["intake"]
        result = template.format(id="WL-42")
        assert result == 'opencode run "/plan WL-42"'

    def test_implement_template(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        template = cmd.dispatch_map["plan"]
        result = template.format(id="WL-42")
        assert result == 'opencode run "work on WL-42 using the implement skill"'

    def test_missing_alias_returns_none(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        assert cmd.dispatch_map.get("unknown") is None

    def test_special_characters_in_id(self):
        desc = _make_descriptor()
        cmd = desc.get_command("delegate")
        template = cmd.dispatch_map["plan"]
        result = template.format(id="SA-0MLX8FNGJ0IYN1LN")
        assert "SA-0MLX8FNGJ0IYN1LN" in result
        assert (
            result
            == 'opencode run "work on SA-0MLX8FNGJ0IYN1LN using the implement skill"'
        )

    def test_command_without_dispatch_map(self):
        """Commands without dispatch_map have an empty dict."""
        cmd = Command(
            name="approve",
            description="Approve work",
            from_states=("review",),
            to="done",
            actor="PM",
        )
        assert cmd.dispatch_map == {}
