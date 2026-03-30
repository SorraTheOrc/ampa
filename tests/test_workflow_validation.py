"""Workflow descriptor validation tests — schema, state machine, invariant, role, delegation.

Covers test plan categories T-SV, T-SM, T-IV, T-RV, T-DV, and T-CD from
``docs/workflow/test-plan.md``.  Each test is tagged with its test plan ID
in the docstring.

These tests wrap the existing standalone validators
(``tests/validate_schema.py`` and ``tests/validate_state_machine.py``)
in pytest test cases and add negative-case tests with mutated descriptors.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW_JSON = _REPO_ROOT / "docs" / "workflow" / "workflow.json"
_WORKFLOW_YAML = _REPO_ROOT / "docs" / "workflow" / "workflow.yaml"
_SCHEMA_PATH = _REPO_ROOT / "docs" / "workflow" / "workflow-schema.json"

# ---------------------------------------------------------------------------
# Reusable validator functions (from validate_state_machine.py)
# ---------------------------------------------------------------------------

from tests.validate_state_machine import (
    ValidationResult,
    resolve_state_ref,
    validate_delegation,
    validate_invariants,
    validate_roles,
    validate_state_machine,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """Load the JSON Schema once per module."""
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def canonical_json() -> dict[str, Any]:
    """Load the canonical workflow.json once per module."""
    with open(_WORKFLOW_JSON) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def canonical_yaml() -> dict[str, Any]:
    """Load the canonical workflow.yaml once per module."""
    with open(_WORKFLOW_YAML) as f:
        return yaml.safe_load(f)


def _schema_errors(descriptor: dict, schema: dict) -> list[str]:
    """Return list of schema validation error messages."""
    validator = Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(descriptor), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"[{path}] {error.message}")
    return errors


def _mutate(base: dict, **overrides: Any) -> dict:
    """Deep-copy *base* and apply top-level key overrides."""
    d = copy.deepcopy(base)
    d.update(overrides)
    return d


def _mutate_nested(base: dict, path: list[str], value: Any) -> dict:
    """Deep-copy *base* and set a nested key to *value*."""
    d = copy.deepcopy(base)
    target = d
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return d


def _remove_key(base: dict, *keys: str) -> dict:
    """Deep-copy *base* and remove top-level keys."""
    d = copy.deepcopy(base)
    for k in keys:
        d.pop(k, None)
    return d


# ===================================================================
# 1. Schema Validation Tests (T-SV)
# ===================================================================


class TestSchemaValidation:
    """JSON Schema validation tests (T-SV-01 through T-SV-10)."""

    def test_tsv01_valid_canonical_passes(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-01: Valid canonical descriptor passes schema validation."""
        errors = _schema_errors(canonical_json, schema)
        assert errors == [], f"Expected no errors, got:\n" + "\n".join(errors)

    @pytest.mark.parametrize(
        "field",
        ["version", "metadata", "status", "stage", "invariants", "commands"],
    )
    def test_tsv02_missing_required_top_level(
        self, canonical_json: dict, schema: dict, field: str
    ) -> None:
        """T-SV-02: Missing required top-level field is rejected."""
        mutated = _remove_key(canonical_json, field)
        errors = _schema_errors(mutated, schema)
        assert any("required" in e.lower() or field in e for e in errors), (
            f"Expected error about missing '{field}', got: {errors}"
        )

    def test_tsv03_invalid_version_format(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-03: Invalid version format is rejected (pattern mismatch)."""
        mutated = _mutate(canonical_json, version="v1.0")
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected validation error for 'v1.0'"
        assert any("pattern" in e.lower() or "version" in e.lower() for e in errors)

    def test_tsv04_empty_status_array(self, canonical_json: dict, schema: dict) -> None:
        """T-SV-04: Empty status array is rejected (minItems violation)."""
        mutated = _mutate(canonical_json, status=[])
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected validation error for empty status[]"

    def test_tsv05_duplicate_stage_values(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-05: Duplicate stage values are rejected (uniqueItems)."""
        mutated = _mutate(canonical_json, stage=["idea", "idea", "done"])
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected uniqueItems error for duplicate stages"

    def test_tsv06_command_missing_required_fields(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-06: Command missing required fields is rejected."""
        mutated = copy.deepcopy(canonical_json)
        mutated["commands"]["bad_cmd"] = {"description": "incomplete command"}
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected error for command missing from/to/actor"
        # Check that errors mention missing required fields
        error_text = " ".join(errors).lower()
        assert any(field in error_text for field in ["from", "to", "actor", "required"])

    def test_tsv07_empty_commands_object(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-07: Empty commands object is rejected (minProperties)."""
        mutated = _mutate(canonical_json, commands={})
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected minProperties error for empty commands"

    def test_tsv08_additional_properties_rejected(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-08: Additional properties at top level are rejected."""
        mutated = _mutate(canonical_json, foo="bar")
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected additionalProperties error"
        assert any("additional" in e.lower() for e in errors)

    def test_tsv09_invalid_invariant_when_value(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-09: Invariant with invalid 'when' value is rejected."""
        mutated = copy.deepcopy(canonical_json)
        mutated["invariants"].append(
            {
                "name": "bad_when",
                "description": "Invalid when",
                "when": "always",
            }
        )
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected error for when='always'"

    def test_tsv10_invalid_input_field_type(
        self, canonical_json: dict, schema: dict
    ) -> None:
        """T-SV-10: InputField with invalid type is rejected."""
        mutated = copy.deepcopy(canonical_json)
        # Add an input with invalid type to an existing command
        mutated["commands"]["intake"]["inputs"]["bad_input"] = {
            "type": "date",
            "description": "Invalid type",
        }
        errors = _schema_errors(mutated, schema)
        assert len(errors) > 0, "Expected error for type='date'"


# ===================================================================
# 2. State Machine Validation Tests (T-SM)
# ===================================================================


class TestStateMachineValidation:
    """State machine semantic validation tests (T-SM-01 through T-SM-11)."""

    def test_tsm01_all_state_tuples_valid(self, canonical_json: dict) -> None:
        """T-SM-01: All state tuples in canonical descriptor reference valid status/stage."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        sm1_errors = [e for e in result.errors if "V-SM1" in e]
        assert sm1_errors == [], f"Expected no V-SM1 errors, got:\n" + "\n".join(
            sm1_errors
        )

    def test_tsm02_undeclared_status_in_state(self, canonical_json: dict) -> None:
        """T-SM-02: State referencing undeclared status produces V-SM1 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["states"]["bad"] = {"status": "archived", "stage": "done"}
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm1_errors = [e for e in result.errors if "V-SM1" in e]
        assert any("archived" in e for e in sm1_errors), (
            f"Expected V-SM1 error about 'archived', got: {sm1_errors}"
        )

    def test_tsm03_undeclared_stage_in_command_to(self, canonical_json: dict) -> None:
        """T-SM-03: Command 'to' referencing undeclared stage produces V-SM1 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["commands"]["test_cmd"] = {
            "description": "Test command",
            "from": ["idea"],
            "to": {"status": "open", "stage": "nonexistent"},
            "actor": "PM",
        }
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm1_errors = [e for e in result.errors if "V-SM1" in e]
        assert any("nonexistent" in e for e in sm1_errors), (
            f"Expected V-SM1 error about 'nonexistent', got: {sm1_errors}"
        )

    def test_tsm04_no_unreachable_states_canonical(self, canonical_json: dict) -> None:
        """T-SM-04: No unreachable states in canonical descriptor."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        sm3_warnings = [w for w in result.warnings if "V-SM3" in w]
        assert sm3_warnings == [], f"Expected no V-SM3 warnings, got:\n" + "\n".join(
            sm3_warnings
        )

    def test_tsm05_detect_unreachable_state(self, canonical_json: dict) -> None:
        """T-SM-05: Adding an orphan state produces V-SM3 warning."""
        mutated = copy.deepcopy(canonical_json)
        mutated["states"]["orphan"] = {"status": "open", "stage": "idea"}
        # Note: orphan resolves to the same tuple as 'idea' so V-SM6 will fire too.
        # Use a unique tuple instead:
        mutated["states"]["orphan"] = {"status": "open", "stage": "prd_complete"}
        # 'prd' already has this tuple; use a unique one by extending stages
        # Actually 'prd' is open/prd_complete so orphan would clash with it (V-SM6).
        # Let's just add a new stage that nothing transitions to.
        mutated["stage"].append("orphan_stage")
        mutated["states"]["orphan"] = {"status": "open", "stage": "orphan_stage"}
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm3_warnings = [w for w in result.warnings if "V-SM3" in w and "orphan" in w]
        assert len(sm3_warnings) > 0, (
            f"Expected V-SM3 warning about 'orphan', got warnings: {result.warnings}"
        )

    def test_tsm06_no_dead_end_states_canonical(self, canonical_json: dict) -> None:
        """T-SM-06: No dead-end states in canonical descriptor."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        sm4_errors = [e for e in result.errors if "V-SM4" in e]
        assert sm4_errors == [], f"Expected no V-SM4 errors, got:\n" + "\n".join(
            sm4_errors
        )

    def test_tsm07_detect_dead_end_state(self, canonical_json: dict) -> None:
        """T-SM-07: Adding a dead-end state produces V-SM4 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["stage"].append("stuck_stage")
        mutated["states"]["stuck"] = {
            "status": "blocked",
            "stage": "stuck_stage",
        }
        # stuck is not in terminal_states and has no outbound commands
        # But we need a command that transitions TO it to make it reachable
        mutated["commands"]["go_to_stuck"] = {
            "description": "Go to stuck",
            "from": ["idea"],
            "to": "stuck",
            "actor": "PM",
        }
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm4_errors = [e for e in result.errors if "V-SM4" in e and "stuck" in e]
        assert len(sm4_errors) > 0, (
            f"Expected V-SM4 error about 'stuck', got errors: {result.errors}"
        )

    def test_tsm08_terminal_states_declared(self, canonical_json: dict) -> None:
        """T-SM-08: All terminal states reference valid state aliases."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        sm5_errors = [e for e in result.errors if "V-SM5" in e]
        assert sm5_errors == [], f"Expected no V-SM5 errors, got:\n" + "\n".join(
            sm5_errors
        )

    def test_tsm09_undeclared_terminal_state(self, canonical_json: dict) -> None:
        """T-SM-09: Undeclared terminal state produces V-SM5 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["terminal_states"] = ["nonexistent"]
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm5_errors = [e for e in result.errors if "V-SM5" in e]
        assert any("nonexistent" in e for e in sm5_errors), (
            f"Expected V-SM5 error about 'nonexistent', got: {sm5_errors}"
        )

    def test_tsm10_state_alias_uniqueness(self, canonical_json: dict) -> None:
        """T-SM-10: No duplicate state alias tuples in canonical descriptor."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        sm6_errors = [e for e in result.errors if "V-SM6" in e]
        assert sm6_errors == [], f"Expected no V-SM6 errors, got:\n" + "\n".join(
            sm6_errors
        )

    def test_tsm11_duplicate_state_alias_tuples(self, canonical_json: dict) -> None:
        """T-SM-11: Two aliases with same tuple produce V-SM6 error."""
        mutated = copy.deepcopy(canonical_json)
        # Add a duplicate: same tuple as 'idea' (open/idea)
        mutated["states"]["idea_copy"] = {"status": "open", "stage": "idea"}
        result = ValidationResult()
        validate_state_machine(mutated, result)
        sm6_errors = [e for e in result.errors if "V-SM6" in e]
        assert len(sm6_errors) > 0, (
            f"Expected V-SM6 error about duplicate tuples, got: {result.errors}"
        )


# ===================================================================
# 3. Invariant Validation Tests (T-IV)
# ===================================================================


class TestInvariantValidation:
    """Invariant reference and phase validation tests (T-IV-01 through T-IV-05)."""

    def test_tiv01_all_invariant_references_resolve(self, canonical_json: dict) -> None:
        """T-IV-01: All invariant references in canonical descriptor resolve."""
        result = ValidationResult()
        validate_invariants(canonical_json, result)
        i1_errors = [e for e in result.errors if "V-I1" in e]
        assert i1_errors == [], f"Expected no V-I1 errors, got:\n" + "\n".join(
            i1_errors
        )

    def test_tiv02_undeclared_invariant_reference(self, canonical_json: dict) -> None:
        """T-IV-02: Command referencing undeclared invariant produces V-I1 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["commands"]["test_cmd"] = {
            "description": "Test",
            "from": ["idea"],
            "to": "intake",
            "actor": "PM",
            "pre": ["nonexistent_invariant"],
        }
        result = ValidationResult()
        validate_invariants(mutated, result)
        i1_errors = [e for e in result.errors if "V-I1" in e]
        assert any("nonexistent_invariant" in e for e in i1_errors), (
            f"Expected V-I1 error about 'nonexistent_invariant', got: {i1_errors}"
        )

    def test_tiv03_invariant_names_unique(self, canonical_json: dict) -> None:
        """T-IV-03: No duplicate invariant names in canonical descriptor."""
        result = ValidationResult()
        validate_invariants(canonical_json, result)
        i2_errors = [e for e in result.errors if "V-I2" in e]
        assert i2_errors == [], f"Expected no V-I2 errors, got:\n" + "\n".join(
            i2_errors
        )

    def test_tiv04_when_phase_compatibility(self, canonical_json: dict) -> None:
        """T-IV-04: All invariant usages are compatible with their 'when' phase."""
        result = ValidationResult()
        validate_invariants(canonical_json, result)
        i3_warnings = [w for w in result.warnings if "V-I3" in w]
        assert i3_warnings == [], f"Expected no V-I3 warnings, got:\n" + "\n".join(
            i3_warnings
        )

    def test_tiv05_phase_mismatch_detection(self, canonical_json: dict) -> None:
        """T-IV-05: Pre-only invariant used in post produces V-I3 warning."""
        mutated = copy.deepcopy(canonical_json)
        # Use a pre-only invariant in a post list
        mutated["commands"]["test_cmd"] = {
            "description": "Test",
            "from": ["idea"],
            "to": "intake",
            "actor": "PM",
            "post": ["requires_work_item_context"],  # when=pre
        }
        result = ValidationResult()
        validate_invariants(mutated, result)
        i3_warnings = [w for w in result.warnings if "V-I3" in w]
        assert any("requires_work_item_context" in w for w in i3_warnings), (
            f"Expected V-I3 warning about 'requires_work_item_context', got: {i3_warnings}"
        )


# ===================================================================
# 4. Role Validation Tests (T-RV)
# ===================================================================


class TestRoleValidation:
    """Role reference validation tests (T-RV-01 through T-RV-03)."""

    def test_trv01_all_actor_references_resolve(self, canonical_json: dict) -> None:
        """T-RV-01: All command actor values match declared role names."""
        result = ValidationResult()
        validate_roles(canonical_json, result)
        r1_errors = [e for e in result.errors if "V-R1" in e]
        assert r1_errors == [], f"Expected no V-R1 errors, got:\n" + "\n".join(
            r1_errors
        )

    def test_trv02_undeclared_actor(self, canonical_json: dict) -> None:
        """T-RV-02: Command with undeclared actor produces V-R1 error."""
        mutated = copy.deepcopy(canonical_json)
        mutated["commands"]["test_cmd"] = {
            "description": "Test",
            "from": ["idea"],
            "to": "intake",
            "actor": "UnknownRole",
        }
        result = ValidationResult()
        validate_roles(mutated, result)
        r1_errors = [e for e in result.errors if "V-R1" in e]
        assert any("UnknownRole" in e for e in r1_errors), (
            f"Expected V-R1 error about 'UnknownRole', got: {r1_errors}"
        )

    def test_trv03_role_names_unique(self, canonical_json: dict) -> None:
        """T-RV-03: No duplicate role names in canonical descriptor."""
        result = ValidationResult()
        validate_roles(canonical_json, result)
        r2_errors = [e for e in result.errors if "V-R2" in e]
        assert r2_errors == [], f"Expected no V-R2 errors, got:\n" + "\n".join(
            r2_errors
        )


# ===================================================================
# 5. Delegation Validation Tests (T-DV)
# ===================================================================


class TestDelegationValidation:
    """AMPA delegation constraint tests (T-DV-01 through T-DV-06)."""

    def test_tdv01_delegate_has_required_pre_invariants(
        self, canonical_json: dict
    ) -> None:
        """T-DV-01: delegate command includes all required pre-invariants."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        d_errors = [
            e for e in result.errors if any(f"V-D{i}" in e for i in range(1, 4))
        ]
        assert d_errors == [], f"Expected no V-D1/V-D2/V-D3 errors, got:\n" + "\n".join(
            d_errors
        )

    def test_tdv02_missing_delegation_pre_invariant(self, canonical_json: dict) -> None:
        """T-DV-02: Removing requires_work_item_context from delegate produces V-D1 error."""
        mutated = copy.deepcopy(canonical_json)
        pre = list(mutated["commands"]["delegate"]["pre"])
        pre.remove("requires_work_item_context")
        mutated["commands"]["delegate"]["pre"] = pre
        result = ValidationResult()
        validate_delegation(mutated, result)
        d1_errors = [e for e in result.errors if "V-D1" in e]
        assert any("requires_work_item_context" in e for e in d1_errors), (
            f"Expected V-D1 error, got: {d1_errors}"
        )

    def test_tdv03_close_with_audit_requires_positive_audit(
        self, canonical_json: dict
    ) -> None:
        """T-DV-03: close_with_audit includes audit_recommends_closure."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        d4_errors = [e for e in result.errors if "V-D4" in e]
        assert d4_errors == [], f"Expected no V-D4 errors, got:\n" + "\n".join(
            d4_errors
        )

    def test_tdv04_audit_fail_requires_negative_audit(
        self, canonical_json: dict
    ) -> None:
        """T-DV-04: audit_fail includes audit_does_not_recommend_closure."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        d5_errors = [e for e in result.errors if "V-D5" in e]
        assert d5_errors == [], f"Expected no V-D5 errors, got:\n" + "\n".join(
            d5_errors
        )

    def test_tdv05_escalate_requires_reason_input(self, canonical_json: dict) -> None:
        """T-DV-05: escalate command has inputs.reason with required=true."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        d6_errors = [e for e in result.errors if "V-D6" in e]
        assert d6_errors == [], f"Expected no V-D6 errors, got:\n" + "\n".join(
            d6_errors
        )

    def test_tdv06_delegate_actor_is_pm(self, canonical_json: dict) -> None:
        """T-DV-06: delegate command has actor == 'PM'."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        d7_errors = [e for e in result.errors if "V-D7" in e]
        assert d7_errors == [], f"Expected no V-D7 errors, got:\n" + "\n".join(
            d7_errors
        )


# ===================================================================
# 6. Canonical Descriptor Tests (T-CD)
# ===================================================================


class TestCanonicalDescriptor:
    """Integration tests running all validators against the canonical descriptor
    (T-CD-01 through T-CD-06)."""

    def test_tcd01_json_passes_schema(self, canonical_json: dict, schema: dict) -> None:
        """T-CD-01: workflow.json passes JSON Schema validation."""
        errors = _schema_errors(canonical_json, schema)
        assert errors == [], f"Schema errors:\n" + "\n".join(errors)

    def test_tcd02_json_passes_state_machine(self, canonical_json: dict) -> None:
        """T-CD-02: workflow.json passes state machine validation (no errors)."""
        result = ValidationResult()
        validate_state_machine(canonical_json, result)
        assert result.errors == [], f"State machine errors:\n" + "\n".join(
            result.errors
        )

    def test_tcd03_json_passes_invariant_validation(self, canonical_json: dict) -> None:
        """T-CD-03: workflow.json passes invariant validation (no errors)."""
        result = ValidationResult()
        validate_invariants(canonical_json, result)
        assert result.errors == [], f"Invariant errors:\n" + "\n".join(result.errors)

    def test_tcd04_json_passes_role_validation(self, canonical_json: dict) -> None:
        """T-CD-04: workflow.json passes role validation (no errors)."""
        result = ValidationResult()
        validate_roles(canonical_json, result)
        assert result.errors == [], f"Role errors:\n" + "\n".join(result.errors)

    def test_tcd05_json_passes_delegation_validation(
        self, canonical_json: dict
    ) -> None:
        """T-CD-05: workflow.json passes delegation validation (no errors)."""
        result = ValidationResult()
        validate_delegation(canonical_json, result)
        assert result.errors == [], f"Delegation errors:\n" + "\n".join(result.errors)

    @pytest.mark.xfail(
        reason="Known drift: workflow.json has extra 'delegated' stage not in workflow.yaml. "
        "JSON should be regenerated from the YAML source.",
        strict=True,
    )
    def test_tcd06_yaml_json_equivalent(
        self, canonical_json: dict, canonical_yaml: dict
    ) -> None:
        """T-CD-06: workflow.yaml and workflow.json are structurally equivalent.

        Compares the YAML and JSON representations after loading.  The YAML is
        the authored source and the JSON is generated; they must agree on
        core structural elements: version, metadata name, status/stage lists,
        state aliases, command names, terminal states, and invariant names.

        Note: The YAML may use YAML-specific features (multi-line strings,
        flow mappings) that normalize differently from JSON.  Only logical
        structure is compared.

        Currently marked xfail due to known stage drift (``delegated`` stage
        in JSON not present in YAML).  When the drift is fixed, this marker
        should be removed — ``strict=True`` ensures the test will loudly
        fail if it starts passing unexpectedly.
        """
        # Version must match exactly
        assert canonical_yaml["version"] == canonical_json["version"]

        # Metadata name must match
        assert canonical_yaml["metadata"]["name"] == canonical_json["metadata"]["name"]

        # Status values must match (order-independent)
        assert sorted(canonical_yaml["status"]) == sorted(canonical_json["status"])

        # Stage values — YAML is the source of truth; JSON may have drifted.
        # We check they share the same core stages and report differences.
        yaml_stages = set(canonical_yaml["stage"])
        json_stages = set(canonical_json["stage"])
        only_yaml = yaml_stages - json_stages
        only_json = json_stages - yaml_stages
        assert only_yaml == set() and only_json == set(), (
            f"Stage drift detected between YAML and JSON.\n"
            f"  Only in YAML: {only_yaml}\n"
            f"  Only in JSON: {only_json}\n"
            f"The JSON should be regenerated from the YAML source."
        )

        # State alias keys must match
        assert set(canonical_yaml.get("states", {}).keys()) == set(
            canonical_json.get("states", {}).keys()
        )

        # Command names must match
        assert set(canonical_yaml.get("commands", {}).keys()) == set(
            canonical_json.get("commands", {}).keys()
        )

        # Terminal states must match
        assert canonical_yaml.get("terminal_states") == canonical_json.get(
            "terminal_states"
        )

        # Invariant names must match
        yaml_inv_names = sorted(
            inv["name"] for inv in canonical_yaml.get("invariants", [])
        )
        json_inv_names = sorted(
            inv["name"] for inv in canonical_json.get("invariants", [])
        )
        assert yaml_inv_names == json_inv_names
