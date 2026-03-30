"""Unit tests for ampa.engine.descriptor — workflow descriptor loader.

Covers: valid load (YAML & JSON), invalid schema, missing required fields,
alias resolution, command lookup by state, invariant lookup, role lookup.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from ampa.engine.descriptor import (
    Command,
    DescriptorValidationError,
    Effects,
    InputField,
    Invariant,
    Metadata,
    Notification,
    Role,
    StateTuple,
    WorkflowDescriptor,
    load_descriptor,
)

# ---------------------------------------------------------------------------
# Paths to real workflow files
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_YAML = REPO_ROOT / "docs" / "workflow" / "workflow.yaml"
WORKFLOW_JSON = REPO_ROOT / "docs" / "workflow" / "workflow.json"
SCHEMA_JSON = REPO_ROOT / "docs" / "workflow" / "workflow-schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_descriptor() -> dict:
    """Return a minimal valid descriptor dict for testing."""
    return {
        "version": "1.0.0",
        "metadata": {
            "name": "test_workflow",
            "description": "A test workflow",
            "owner": "test-team",
            "roles": [
                {"name": "PM", "description": "Product manager", "type": "either"}
            ],
        },
        "status": ["open", "closed"],
        "stage": ["idea", "done"],
        "states": {
            "idea": {"status": "open", "stage": "idea"},
            "shipped": {"status": "closed", "stage": "done"},
        },
        "terminal_states": ["shipped"],
        "invariants": [
            {
                "name": "has_description",
                "description": "Must have a description",
                "when": "pre",
                "logic": "length(description) > 10",
            }
        ],
        "commands": {
            "start": {
                "description": "Start working",
                "from": ["idea"],
                "to": "shipped",
                "actor": "PM",
                "pre": ["has_description"],
            }
        },
    }


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f)


def _write_json(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Test: Load real workflow.yaml
# ---------------------------------------------------------------------------


class TestLoadRealWorkflow:
    """Load the actual ``docs/workflow/workflow.yaml`` and verify key properties."""

    @pytest.fixture(scope="class")
    def descriptor(self) -> WorkflowDescriptor:
        return load_descriptor(WORKFLOW_YAML, schema_path=SCHEMA_JSON)

    def test_version(self, descriptor: WorkflowDescriptor) -> None:
        assert descriptor.version == "1.0.0"

    def test_metadata_name(self, descriptor: WorkflowDescriptor) -> None:
        assert descriptor.metadata.name == "ampa_prd_workflow"

    def test_statuses(self, descriptor: WorkflowDescriptor) -> None:
        assert descriptor.statuses == (
            "open",
            "in_progress",
            "blocked",
            "completed",
            "closed",
        )

    def test_stages(self, descriptor: WorkflowDescriptor) -> None:
        assert "idea" in descriptor.stages
        assert "done" in descriptor.stages
        assert len(descriptor.stages) == 10  # delegated removed; uses in_progress + tag

    def test_state_aliases(self, descriptor: WorkflowDescriptor) -> None:
        assert "idea" in descriptor.states
        assert "delegated" in descriptor.states
        idea = descriptor.resolve_alias("idea")
        assert idea == StateTuple(status="open", stage="idea")

    def test_terminal_states(self, descriptor: WorkflowDescriptor) -> None:
        assert descriptor.terminal_states == ("shipped",)

    def test_invariant_count(self, descriptor: WorkflowDescriptor) -> None:
        # workflow.yaml defines 10 invariants
        assert len(descriptor.invariants) >= 10

    def test_command_count(self, descriptor: WorkflowDescriptor) -> None:
        # workflow.yaml defines 17+ commands
        assert len(descriptor.commands) >= 17

    def test_roles(self, descriptor: WorkflowDescriptor) -> None:
        producer = descriptor.get_role("Producer")
        assert producer.type == "human"
        patch = descriptor.get_role("Patch")
        assert patch.type == "agent"
        pm = descriptor.get_role("PM")
        assert pm.type == "either"

    def test_delegate_command(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("delegate")
        assert cmd.actor == "PM"
        assert len(cmd.pre) == 5
        assert "requires_work_item_context" in cmd.pre
        assert "not_do_not_delegate" in cmd.pre
        assert cmd.effects is not None
        assert cmd.effects.set_assignee == "Patch"
        assert "delegated" in cmd.effects.add_tags

    def test_delegate_inputs(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("delegate")
        assert "work_item_id" in cmd.inputs
        assert "action" in cmd.inputs
        action_input = cmd.inputs["action"]
        assert action_input.required is True
        assert action_input.enum == ("intake", "plan", "implement")

    def test_delegate_from_states(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("delegate")
        # delegate from: [idea, intake, plan]
        assert len(cmd.from_states) == 3

    def test_delegate_dispatch_map(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("delegate")
        assert len(cmd.dispatch_map) == 3
        assert "idea" in cmd.dispatch_map
        assert "intake" in cmd.dispatch_map
        assert "plan" in cmd.dispatch_map
        assert "/intake" in cmd.dispatch_map["idea"]
        assert "/plan" in cmd.dispatch_map["intake"]
        assert "implement" in cmd.dispatch_map["plan"]

    def test_delegate_dispatch_map_template_substitution(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmd = descriptor.get_command("delegate")
        result = cmd.dispatch_map["idea"].format(id="WL-99")
        assert "WL-99" in result
        assert result == 'opencode run "/intake WL-99 do not ask questions"'

    def test_command_without_dispatch_map(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("intake")
        assert cmd.dispatch_map == {}

    def test_resolve_from_state_alias_idea(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmd = descriptor.get_command("delegate")
        state = descriptor.resolve_alias("idea")
        assert descriptor.resolve_from_state_alias(cmd, state) == "idea"

    def test_resolve_from_state_alias_intake(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmd = descriptor.get_command("delegate")
        state = descriptor.resolve_alias("intake")
        assert descriptor.resolve_from_state_alias(cmd, state) == "intake"

    def test_resolve_from_state_alias_plan(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmd = descriptor.get_command("delegate")
        state = descriptor.resolve_alias("plan")
        assert descriptor.resolve_from_state_alias(cmd, state) == "plan"

    def test_resolve_from_state_alias_no_match(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmd = descriptor.get_command("delegate")
        state = StateTuple(status="in_progress", stage="in_review")
        assert descriptor.resolve_from_state_alias(cmd, state) is None

    def test_close_with_audit_inline_to(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("close_with_audit")
        # close_with_audit uses inline StateTuple for "to"
        assert isinstance(cmd.to, StateTuple)
        assert cmd.to.status == "completed"
        assert cmd.to.stage == "in_review"

    def test_commands_for_state(self, descriptor: WorkflowDescriptor) -> None:
        # idea state (open, idea) should have at least the intake command
        cmds = descriptor.commands_for_state("open", "idea")
        cmd_names = [c.name for c in cmds]
        assert "intake" in cmd_names
        assert "delegate" in cmd_names

    def test_invariant_lookup(self, descriptor: WorkflowDescriptor) -> None:
        invs = descriptor.get_invariants(
            ["requires_work_item_context", "not_do_not_delegate"]
        )
        assert len(invs) == 2
        assert invs[0].name == "requires_work_item_context"
        assert invs[0].when == ("pre",)

    def test_invariant_when_post(self, descriptor: WorkflowDescriptor) -> None:
        invs = descriptor.get_invariants(["requires_approvals"])
        assert invs[0].when == ("post",)


# ---------------------------------------------------------------------------
# Test: Load real workflow.json
# ---------------------------------------------------------------------------


class TestLoadRealWorkflowJSON:
    """Ensure the JSON version also loads successfully."""

    @pytest.mark.skipif(not WORKFLOW_JSON.exists(), reason="workflow.json not present")
    def test_load_json(self) -> None:
        desc = load_descriptor(WORKFLOW_JSON, schema_path=SCHEMA_JSON)
        assert desc.version == "1.0.0"
        assert len(desc.commands) >= 17


# ---------------------------------------------------------------------------
# Test: Minimal descriptor (YAML)
# ---------------------------------------------------------------------------


class TestMinimalDescriptor:
    def test_load_minimal_yaml(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        yaml_path = tmp_path / "workflow.yaml"
        _write_yaml(yaml_path, data)
        desc = load_descriptor(yaml_path, schema_path=SCHEMA_JSON)
        assert desc.version == "1.0.0"
        assert desc.metadata.name == "test_workflow"
        assert len(desc.commands) == 1
        assert len(desc.invariants) == 1

    def test_load_minimal_json(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        json_path = tmp_path / "workflow.json"
        _write_json(json_path, data)
        desc = load_descriptor(json_path, schema_path=SCHEMA_JSON)
        assert desc.version == "1.0.0"

    def test_frozen_dataclasses(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        yaml_path = tmp_path / "workflow.yaml"
        _write_yaml(yaml_path, data)
        desc = load_descriptor(yaml_path, schema_path=SCHEMA_JSON)
        with pytest.raises(AttributeError):
            desc.version = "2.0.0"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test: Schema validation errors
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_missing_version(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        del data["version"]
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError) as exc_info:
            load_descriptor(path, schema_path=SCHEMA_JSON)
        assert "'version' is a required property" in str(exc_info.value)

    def test_missing_metadata(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        del data["metadata"]
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError) as exc_info:
            load_descriptor(path, schema_path=SCHEMA_JSON)
        assert "'metadata' is a required property" in str(exc_info.value)

    def test_missing_commands(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        del data["commands"]
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError) as exc_info:
            load_descriptor(path, schema_path=SCHEMA_JSON)
        assert "'commands' is a required property" in str(exc_info.value)

    def test_invalid_version_format(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["version"] = "not-semver"
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError) as exc_info:
            load_descriptor(path, schema_path=SCHEMA_JSON)
        assert "version" in str(exc_info.value).lower()

    def test_empty_status_array(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["status"] = []
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError):
            load_descriptor(path, schema_path=SCHEMA_JSON)

    def test_multiple_errors_reported(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        del data["version"]
        del data["metadata"]
        path = tmp_path / "bad.yaml"
        _write_yaml(path, data)
        with pytest.raises(DescriptorValidationError) as exc_info:
            load_descriptor(path, schema_path=SCHEMA_JSON)
        assert len(exc_info.value.errors) >= 2


# ---------------------------------------------------------------------------
# Test: File errors
# ---------------------------------------------------------------------------


class TestFileErrors:
    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_descriptor("/nonexistent/workflow.yaml")

    def test_unsupported_format(self, tmp_path: Path) -> None:
        path = tmp_path / "workflow.txt"
        path.write_text("hello")
        with pytest.raises(ValueError, match="Unsupported descriptor format"):
            load_descriptor(path)


# ---------------------------------------------------------------------------
# Test: Alias resolution
# ---------------------------------------------------------------------------


class TestAliasResolution:
    @pytest.fixture
    def descriptor(self, tmp_path: Path) -> WorkflowDescriptor:
        data = _minimal_descriptor()
        path = tmp_path / "workflow.yaml"
        _write_yaml(path, data)
        return load_descriptor(path, schema_path=SCHEMA_JSON)

    def test_resolve_existing_alias(self, descriptor: WorkflowDescriptor) -> None:
        st = descriptor.resolve_alias("idea")
        assert st == StateTuple(status="open", stage="idea")

    def test_resolve_unknown_alias(self, descriptor: WorkflowDescriptor) -> None:
        with pytest.raises(KeyError, match="Unknown state alias 'bogus'"):
            descriptor.resolve_alias("bogus")


# ---------------------------------------------------------------------------
# Test: Command lookup by state
# ---------------------------------------------------------------------------


class TestCommandLookup:
    @pytest.fixture
    def descriptor(self, tmp_path: Path) -> WorkflowDescriptor:
        data = _minimal_descriptor()
        path = tmp_path / "workflow.yaml"
        _write_yaml(path, data)
        return load_descriptor(path, schema_path=SCHEMA_JSON)

    def test_commands_for_matching_state(self, descriptor: WorkflowDescriptor) -> None:
        cmds = descriptor.commands_for_state("open", "idea")
        assert len(cmds) == 1
        assert cmds[0].name == "start"

    def test_commands_for_nonmatching_state(
        self, descriptor: WorkflowDescriptor
    ) -> None:
        cmds = descriptor.commands_for_state("closed", "done")
        assert cmds == []

    def test_get_command_by_name(self, descriptor: WorkflowDescriptor) -> None:
        cmd = descriptor.get_command("start")
        assert cmd.name == "start"
        assert cmd.actor == "PM"

    def test_get_unknown_command(self, descriptor: WorkflowDescriptor) -> None:
        with pytest.raises(KeyError, match="Unknown command 'nope'"):
            descriptor.get_command("nope")


# ---------------------------------------------------------------------------
# Test: Invariant lookup
# ---------------------------------------------------------------------------


class TestInvariantLookup:
    @pytest.fixture
    def descriptor(self, tmp_path: Path) -> WorkflowDescriptor:
        data = _minimal_descriptor()
        path = tmp_path / "workflow.yaml"
        _write_yaml(path, data)
        return load_descriptor(path, schema_path=SCHEMA_JSON)

    def test_get_invariants(self, descriptor: WorkflowDescriptor) -> None:
        invs = descriptor.get_invariants(["has_description"])
        assert len(invs) == 1
        assert invs[0].logic == "length(description) > 10"

    def test_get_unknown_invariant(self, descriptor: WorkflowDescriptor) -> None:
        with pytest.raises(KeyError, match="Unknown invariant 'missing'"):
            descriptor.get_invariants(["missing"])


# ---------------------------------------------------------------------------
# Test: Role lookup
# ---------------------------------------------------------------------------


class TestRoleLookup:
    @pytest.fixture
    def descriptor(self, tmp_path: Path) -> WorkflowDescriptor:
        data = _minimal_descriptor()
        path = tmp_path / "workflow.yaml"
        _write_yaml(path, data)
        return load_descriptor(path, schema_path=SCHEMA_JSON)

    def test_get_existing_role(self, descriptor: WorkflowDescriptor) -> None:
        role = descriptor.get_role("PM")
        assert role.type == "either"

    def test_get_unknown_role(self, descriptor: WorkflowDescriptor) -> None:
        with pytest.raises(KeyError, match="Unknown role 'Admin'"):
            descriptor.get_role("Admin")


# ---------------------------------------------------------------------------
# Test: Invariant when normalization
# ---------------------------------------------------------------------------


class TestInvariantWhenParsing:
    def test_when_pre(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["invariants"][0]["when"] = "pre"
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        assert desc.invariants[0].when == ("pre",)

    def test_when_post(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["invariants"][0]["when"] = "post"
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        assert desc.invariants[0].when == ("post",)

    def test_when_both(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["invariants"][0]["when"] = "both"
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        assert desc.invariants[0].when == ("pre", "post")

    def test_when_array(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["invariants"][0]["when"] = ["pre", "post"]
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        assert desc.invariants[0].when == ("pre", "post")


# ---------------------------------------------------------------------------
# Test: Effects parsing
# ---------------------------------------------------------------------------


class TestEffectsParsing:
    def test_effects_with_all_fields(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["commands"]["start"]["effects"] = {
            "add_tags": ["a", "b"],
            "remove_tags": ["c"],
            "set_assignee": "Patch",
            "set_needs_producer_review": True,
            "notifications": [{"channel": "discord", "message": "hello ${title}"}],
            "audit": {
                "record_prompt_hash": True,
                "record_model": True,
                "record_response_ids": False,
                "record_agent_id": True,
            },
        }
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        cmd = desc.get_command("start")
        assert cmd.effects is not None
        assert cmd.effects.add_tags == ("a", "b")
        assert cmd.effects.remove_tags == ("c",)
        assert cmd.effects.set_assignee == "Patch"
        assert cmd.effects.set_needs_producer_review is True
        assert len(cmd.effects.notifications) == 1
        assert cmd.effects.notifications[0].channel == "discord"
        assert cmd.effects.audit is not None
        assert cmd.effects.audit.record_prompt_hash is True
        assert cmd.effects.audit.record_response_ids is False

    def test_command_without_effects(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        cmd = desc.get_command("start")
        assert cmd.effects is None


# ---------------------------------------------------------------------------
# Test: Inline StateTuple in command "to"
# ---------------------------------------------------------------------------


class TestInlineStateTuple:
    def test_inline_to_state(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["commands"]["start"]["to"] = {"status": "closed", "stage": "done"}
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        cmd = desc.get_command("start")
        assert isinstance(cmd.to, StateTuple)
        assert cmd.to.status == "closed"
        assert cmd.to.stage == "done"


# ---------------------------------------------------------------------------
# Test: Simple role (string-only) parsing
# ---------------------------------------------------------------------------


class TestSimpleRoleParsing:
    def test_string_role(self, tmp_path: Path) -> None:
        data = _minimal_descriptor()
        data["metadata"]["roles"] = ["SimpleRole"]
        path = tmp_path / "wf.yaml"
        _write_yaml(path, data)
        desc = load_descriptor(path, schema_path=SCHEMA_JSON)
        role = desc.get_role("SimpleRole")
        assert role.name == "SimpleRole"
        assert role.type == "either"
        assert role.description == ""
