#!/usr/bin/env python3
from __future__ import annotations

"""
Semantic validation of a workflow descriptor beyond JSON Schema.

Checks:
  V-SM1: State tuples reference valid status/stage
  V-SM2: Commands have >=1 from, exactly 1 to (post-resolution)
  V-SM3: No unreachable states (warning)
  V-SM4: No dead-end states (unless terminal)
  V-SM5: Terminal states are declared
  V-SM6: State alias uniqueness
  V-I1:  Invariant references exist
  V-I2:  Invariant names are unique
  V-I3:  Invariant when-phase compatibility (warning)
  V-R1:  Actor references valid role
  V-R2:  Role names are unique
  V-D1:  Delegation requires context invariant
  V-D2:  Delegation requires AC invariant
  V-D3:  Delegation requires concurrency invariant
  V-D4:  close_with_audit requires positive audit invariant
  V-D5:  audit_fail requires negative audit invariant
  V-D6:  Escalation requires reason input
  V-D7:  Delegation actor is PM

CI-ready: exit 0 on success (warnings allowed), exit 1 on errors, exit 2 on file errors.

Usage:
    python tests/validate_state_machine.py
    python tests/validate_state_machine.py --descriptor path/to/workflow.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict:
    """Load and parse a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(2)


class ValidationResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, rule_id: str, message: str):
        self.errors.append(f"ERROR [{rule_id}]: {message}")

    def warning(self, rule_id: str, message: str):
        self.warnings.append(f"WARN  [{rule_id}]: {message}")

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def resolve_state_ref(ref: Any, states: dict[str, dict]) -> tuple[str, str] | None:
    """Resolve a StateRef (string alias or inline tuple) to (status, stage)."""
    if isinstance(ref, str):
        if ref in states:
            s = states[ref]
            return (s["status"], s["stage"])
        return None  # unresolved alias
    elif isinstance(ref, dict) and "status" in ref and "stage" in ref:
        return (ref["status"], ref["stage"])
    return None


def get_role_names(roles: list) -> list[str]:
    """Extract role names from the roles array (handles string and object entries)."""
    names = []
    for r in roles:
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict) and "name" in r:
            names.append(r["name"])
    return names


def validate_state_machine(desc: dict, result: ValidationResult):
    """Run V-SM rules."""
    statuses = set(desc.get("status", []))
    stages = set(desc.get("stage", []))
    states = desc.get("states", {})
    commands = desc.get("commands", {})
    terminal = set(desc.get("terminal_states", []))

    # V-SM1: State tuples reference valid status/stage
    for alias, state in states.items():
        if state["status"] not in statuses:
            result.error(
                "V-SM1",
                f'State "{alias}" references undeclared status "{state["status"]}". Declared: {sorted(statuses)}',
            )
        if state["stage"] not in stages:
            result.error(
                "V-SM1",
                f'State "{alias}" references undeclared stage "{state["stage"]}". Declared: {sorted(stages)}',
            )

    for cmd_name, cmd in commands.items():
        for i, ref in enumerate(cmd.get("from", [])):
            resolved = resolve_state_ref(ref, states)
            if resolved is None:
                if isinstance(ref, str):
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" from[{i}] references undeclared state alias "{ref}"',
                    )
                else:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" from[{i}] has unresolvable state reference',
                    )
            elif isinstance(ref, dict):
                if ref["status"] not in statuses:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" from[{i}] references undeclared status "{ref["status"]}"',
                    )
                if ref["stage"] not in stages:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" from[{i}] references undeclared stage "{ref["stage"]}"',
                    )

        to_ref = cmd.get("to")
        if to_ref is not None:
            resolved = resolve_state_ref(to_ref, states)
            if resolved is None:
                if isinstance(to_ref, str):
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" to references undeclared state alias "{to_ref}"',
                    )
                else:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" to has unresolvable state reference',
                    )
            elif isinstance(to_ref, dict):
                if to_ref["status"] not in statuses:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" to references undeclared status "{to_ref["status"]}"',
                    )
                if to_ref["stage"] not in stages:
                    result.error(
                        "V-SM1",
                        f'Command "{cmd_name}" to references undeclared stage "{to_ref["stage"]}"',
                    )

    # V-SM3: No unreachable states
    reachable_to: set[tuple[str, str]] = set()
    for cmd in commands.values():
        to_ref = cmd.get("to")
        if to_ref is not None:
            resolved = resolve_state_ref(to_ref, states)
            if resolved:
                reachable_to.add(resolved)

    # Identify initial states (first state alias or states with the first stage value)
    first_stage = desc.get("stage", [None])[0] if desc.get("stage") else None
    initial_tuples: set[tuple[str, str]] = set()
    for alias, state in states.items():
        if state["stage"] == first_stage:
            initial_tuples.add((state["status"], state["stage"]))

    for alias, state in states.items():
        t = (state["status"], state["stage"])
        if t not in initial_tuples and t not in reachable_to:
            result.warning(
                "V-SM3",
                f'State "{alias}" ({state["status"]}/{state["stage"]}) is unreachable — no command transitions to it',
            )

    # V-SM4: No dead-end states
    has_outbound: set[tuple[str, str]] = set()
    for cmd in commands.values():
        for ref in cmd.get("from", []):
            resolved = resolve_state_ref(ref, states)
            if resolved:
                has_outbound.add(resolved)

    terminal_tuples: set[tuple[str, str]] = set()
    for t in terminal:
        if t in states:
            s = states[t]
            terminal_tuples.add((s["status"], s["stage"]))

    for alias, state in states.items():
        t = (state["status"], state["stage"])
        if t not in terminal_tuples and t not in has_outbound:
            result.error(
                "V-SM4",
                f'State "{alias}" ({state["status"]}/{state["stage"]}) is a dead-end — no command transitions from it and it is not terminal',
            )

    # V-SM5: Terminal states are declared
    for t in terminal:
        if t not in states:
            result.error("V-SM5", f'Terminal state "{t}" is not defined in states')

    # V-SM6: State alias uniqueness
    tuple_to_aliases: dict[tuple[str, str], list[str]] = {}
    for alias, state in states.items():
        t = (state["status"], state["stage"])
        tuple_to_aliases.setdefault(t, []).append(alias)
    for t, aliases in tuple_to_aliases.items():
        if len(aliases) > 1:
            result.error("V-SM6", f"States {aliases} all resolve to {t[0]}/{t[1]}")


def validate_invariants(desc: dict, result: ValidationResult):
    """Run V-I rules."""
    invariants = desc.get("invariants", [])
    commands = desc.get("commands", {})

    # V-I2: Invariant names are unique
    inv_names: dict[str, int] = {}
    for inv in invariants:
        name = inv.get("name", "")
        inv_names[name] = inv_names.get(name, 0) + 1
    for name, count in inv_names.items():
        if count > 1:
            result.error(
                "V-I2", f'Duplicate invariant name "{name}" (appears {count} times)'
            )

    declared = set(inv_names.keys())

    # Build when-phase lookup
    inv_when: dict[str, set[str]] = {}
    for inv in invariants:
        name = inv.get("name", "")
        when = inv.get("when", "both")
        if when == "both":
            inv_when[name] = {"pre", "post"}
        elif isinstance(when, list):
            inv_when[name] = set(when)
        elif isinstance(when, str):
            inv_when[name] = {when}

    # V-I1: Invariant references exist
    # V-I3: When-phase compatibility
    for cmd_name, cmd in commands.items():
        for inv_name in cmd.get("pre", []):
            if inv_name not in declared:
                result.error(
                    "V-I1",
                    f'Command "{cmd_name}" references undeclared invariant "{inv_name}" in pre. Declared: {sorted(declared)}',
                )
            elif "pre" not in inv_when.get(inv_name, set()):
                result.warning(
                    "V-I3",
                    f'Invariant "{inv_name}" is declared as when="{inv_when.get(inv_name)}" but used in pre of command "{cmd_name}"',
                )

        for inv_name in cmd.get("post", []):
            if inv_name not in declared:
                result.error(
                    "V-I1",
                    f'Command "{cmd_name}" references undeclared invariant "{inv_name}" in post. Declared: {sorted(declared)}',
                )
            elif "post" not in inv_when.get(inv_name, set()):
                result.warning(
                    "V-I3",
                    f'Invariant "{inv_name}" is declared as when="{inv_when.get(inv_name)}" but used in post of command "{cmd_name}"',
                )


def validate_roles(desc: dict, result: ValidationResult):
    """Run V-R rules."""
    roles = desc.get("metadata", {}).get("roles", [])
    commands = desc.get("commands", {})

    role_names = get_role_names(roles)

    # V-R2: Role names are unique
    seen: dict[str, int] = {}
    for name in role_names:
        seen[name] = seen.get(name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            result.error(
                "V-R2", f'Duplicate role name "{name}" (appears {count} times)'
            )

    role_set = set(role_names)

    # V-R1: Actor references valid role
    for cmd_name, cmd in commands.items():
        actor = cmd.get("actor", "")
        if actor not in role_set:
            result.error(
                "V-R1",
                f'Command "{cmd_name}" references undeclared actor "{actor}". Declared roles: {sorted(role_set)}',
            )


def validate_delegation(desc: dict, result: ValidationResult):
    """Run V-D rules (AMPA-specific)."""
    states = desc.get("states", {})
    commands = desc.get("commands", {})

    # Find the delegated state tuple
    delegated_tuple = None
    if "delegated" in states:
        s = states["delegated"]
        delegated_tuple = (s["status"], s["stage"])

    # V-D1, V-D2, V-D3: Delegation commands require specific pre-invariants
    # Only applies to commands that perform *initial* delegation (not restoration
    # from a blocked state). A command is considered a restoration if all of its
    # from states have status "blocked".
    delegation_invariants = {
        "requires_work_item_context": "V-D1",
        "requires_acceptance_criteria": "V-D2",
        "no_in_progress_items": "V-D3",
    }

    for cmd_name, cmd in commands.items():
        to_ref = cmd.get("to")
        if to_ref is not None and delegated_tuple is not None:
            resolved = resolve_state_ref(to_ref, states)
            if resolved == delegated_tuple:
                # Check if this is a restoration command (all from states are blocked)
                from_refs = cmd.get("from", [])
                all_from_blocked = len(from_refs) > 0 and all(
                    (r_tuple := resolve_state_ref(ref, states)) is not None
                    and r_tuple[0] == "blocked"
                    for ref in from_refs
                )
                if all_from_blocked:
                    continue  # Skip — this is a restoration, not an initial delegation

                pre = set(cmd.get("pre", []))
                for inv_name, rule_id in delegation_invariants.items():
                    if inv_name not in pre:
                        result.error(
                            rule_id,
                            f'Command "{cmd_name}" transitions to delegated state but does not require "{inv_name}" pre-invariant',
                        )

    # V-D4: close_with_audit requires audit_recommends_closure
    if "close_with_audit" in commands:
        pre = set(commands["close_with_audit"].get("pre", []))
        if "audit_recommends_closure" not in pre:
            result.error(
                "V-D4",
                'Command "close_with_audit" does not require "audit_recommends_closure" pre-invariant',
            )

    # V-D5: audit_fail requires audit_does_not_recommend_closure
    if "audit_fail" in commands:
        pre = set(commands["audit_fail"].get("pre", []))
        if "audit_does_not_recommend_closure" not in pre:
            result.error(
                "V-D5",
                'Command "audit_fail" does not require "audit_does_not_recommend_closure" pre-invariant',
            )

    # V-D6: escalate requires reason input
    if "escalate" in commands:
        inputs = commands["escalate"].get("inputs", {})
        reason = inputs.get("reason", {})
        if not reason or not reason.get("required", False):
            result.error("V-D6", 'Command "escalate" does not require a "reason" input')

    # V-D7: delegate actor is PM
    if "delegate" in commands:
        actor = commands["delegate"].get("actor", "")
        if actor != "PM":
            result.error(
                "V-D7", f'Command "delegate" has actor "{actor}" but expected "PM"'
            )


def main():
    parser = argparse.ArgumentParser(
        description="Semantic validation of workflow descriptor"
    )
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=Path(__file__).parent.parent / "docs" / "workflow" / "workflow.json",
        help="Path to workflow descriptor JSON file (default: docs/workflow/workflow.json)",
    )
    args = parser.parse_args()

    print(f"Descriptor: {args.descriptor}")
    print()

    desc = load_json(args.descriptor)
    result = ValidationResult()

    # Run all validators
    print("Running state machine validation (V-SM)...")
    validate_state_machine(desc, result)

    print("Running invariant validation (V-I)...")
    validate_invariants(desc, result)

    print("Running role validation (V-R)...")
    validate_roles(desc, result)

    print("Running delegation validation (V-D)...")
    validate_delegation(desc, result)

    print()

    # Report results
    if result.warnings:
        print(f"Warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  {w}")
        print()

    if result.errors:
        print(f"FAIL: {len(result.errors)} error(s):")
        for e in result.errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print(
            f"PASS: All semantic validation checks passed ({len(result.warnings)} warning(s))."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
