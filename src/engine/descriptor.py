"""Workflow descriptor loader — parses and validates workflow YAML/JSON files.

Loads a workflow descriptor into an immutable in-memory model and validates it
against the JSON Schema defined in ``docs/workflow/workflow-schema.json``.

Usage::

    from ampa.engine.descriptor import load_descriptor

    descriptor = load_descriptor("docs/workflow/workflow.yaml")
    cmd = descriptor.get_command("delegate")
    state = descriptor.resolve_alias("idea")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence, Union

import yaml
from jsonschema import Draft202012Validator, ValidationError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DescriptorValidationError(Exception):
    """Raised when a workflow descriptor fails schema or structural validation.

    Attributes:
        errors: List of individual validation error messages.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        bullet_list = "\n  - ".join(errors)
        super().__init__(
            f"Workflow descriptor validation failed with {len(errors)} error(s):\n  - {bullet_list}"
        )


# ---------------------------------------------------------------------------
# Data classes — frozen / immutable after construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Role:
    """A role definition from ``metadata.roles``."""

    name: str
    description: str = ""
    type: str = "either"  # "human" | "agent" | "either"


@dataclass(frozen=True)
class StateTuple:
    """An immutable ``(status, stage)`` pair."""

    status: str
    stage: str


@dataclass(frozen=True)
class InputField:
    """A command input parameter definition."""

    type: str  # "string" | "number" | "boolean" | "array" | "object"
    required: bool = False
    description: str = ""
    enum: tuple[Any, ...] = ()
    default: Any = None


@dataclass(frozen=True)
class Notification:
    """A notification entry from command effects."""

    channel: str
    message: str = ""


@dataclass(frozen=True)
class AuditEffects:
    """Audit recording flags from command effects."""

    record_prompt_hash: bool = False
    record_model: bool = False
    record_response_ids: bool = False
    record_agent_id: bool = False


@dataclass(frozen=True)
class Effects:
    """Side effects applied after successful command execution."""

    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    set_assignee: str | None = None
    set_needs_producer_review: bool | None = None
    notifications: tuple[Notification, ...] = ()
    audit: AuditEffects | None = None


@dataclass(frozen=True)
class Invariant:
    """A named invariant rule from the ``invariants`` array."""

    name: str
    description: str
    when: tuple[str, ...]  # subset of ("pre", "post")
    logic: str = ""


# StateRef is either a string alias or an inline StateTuple.
StateRef = Union[str, StateTuple]


@dataclass(frozen=True)
class Command:
    """A command definition from the ``commands`` map."""

    name: str
    description: str
    from_states: tuple[StateRef, ...]
    to: StateRef
    actor: str
    pre: tuple[str, ...] = ()
    post: tuple[str, ...] = ()
    inputs: dict[str, InputField] = field(default_factory=dict)
    prompt_ref: str = ""
    effects: Effects | None = None
    dispatch_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Metadata:
    """Top-level metadata block."""

    name: str
    description: str
    owner: str
    roles: tuple[Role, ...]
    links: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowDescriptor:
    """Immutable in-memory representation of a parsed workflow descriptor.

    Provides query methods for looking up commands, invariants, roles, and
    resolving state aliases.
    """

    version: str
    metadata: Metadata
    statuses: tuple[str, ...]
    stages: tuple[str, ...]
    states: dict[str, StateTuple]
    terminal_states: tuple[str, ...] = ()
    invariants: tuple[Invariant, ...] = ()
    commands: dict[str, Command] = field(default_factory=dict)

    # ---- Query helpers ----------------------------------------------------

    def resolve_alias(self, name: str) -> StateTuple:
        """Resolve a state alias to its ``(status, stage)`` tuple.

        Raises ``KeyError`` if *name* is not a known alias.
        """
        try:
            return self.states[name]
        except KeyError:
            raise KeyError(
                f"Unknown state alias '{name}'. "
                f"Known aliases: {', '.join(sorted(self.states))}"
            )

    def resolve_state_ref(self, ref: StateRef) -> StateTuple:
        """Resolve a ``StateRef`` (alias string *or* inline ``StateTuple``)."""
        if isinstance(ref, str):
            return self.resolve_alias(ref)
        return ref

    def commands_for_state(self, status: str, stage: str) -> list[Command]:
        """Return all commands whose ``from`` list includes the given state.

        State aliases in each command's ``from`` list are resolved before
        comparison.
        """
        target = StateTuple(status=status, stage=stage)
        result: list[Command] = []
        for cmd in self.commands.values():
            for ref in cmd.from_states:
                resolved = self.resolve_state_ref(ref)
                if resolved == target:
                    result.append(cmd)
                    break
        return result

    def resolve_from_state_alias(
        self, command: Command, state: StateTuple
    ) -> str | None:
        """Return the from-state alias in *command* that matches *state*.

        Iterates over *command.from_states*, resolves each ``StateRef`` and
        returns the first alias string whose resolved ``StateTuple`` equals
        *state*.  Returns ``None`` if no match is found or if the matching
        ``StateRef`` is an inline ``StateTuple`` rather than an alias string.
        """
        for ref in command.from_states:
            if isinstance(ref, str):
                try:
                    resolved = self.resolve_alias(ref)
                except KeyError:
                    continue
                if resolved == state:
                    return ref
        return None

    def get_command(self, name: str) -> Command:
        """Look up a command by name.

        Raises ``KeyError`` if *name* is not defined.
        """
        try:
            return self.commands[name]
        except KeyError:
            raise KeyError(
                f"Unknown command '{name}'. "
                f"Known commands: {', '.join(sorted(self.commands))}"
            )

    def get_invariants(self, names: Sequence[str]) -> list[Invariant]:
        """Return ``Invariant`` objects for each name in *names*.

        Raises ``KeyError`` if any name is not defined.
        """
        index = {inv.name: inv for inv in self.invariants}
        result: list[Invariant] = []
        for n in names:
            if n not in index:
                raise KeyError(
                    f"Unknown invariant '{n}'. "
                    f"Known invariants: {', '.join(sorted(index))}"
                )
            result.append(index[n])
        return result

    def get_role(self, name: str) -> Role:
        """Look up a role by name.

        Raises ``KeyError`` if *name* is not defined.
        """
        for role in self.metadata.roles:
            if role.name == name:
                return role
        raise KeyError(
            f"Unknown role '{name}'. "
            f"Known roles: {', '.join(r.name for r in self.metadata.roles)}"
        )


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _parse_role(raw: Any) -> Role:
    """Parse a role entry (string or dict) into a ``Role``."""
    if isinstance(raw, str):
        return Role(name=raw)
    return Role(
        name=raw["name"],
        description=raw.get("description", ""),
        type=raw.get("type", "either"),
    )


def _parse_state_ref(raw: Any) -> StateRef:
    """Parse a state reference (string alias or inline dict)."""
    if isinstance(raw, str):
        return raw
    return StateTuple(status=raw["status"], stage=raw["stage"])


def _parse_when(raw: Any) -> tuple[str, ...]:
    """Normalize the ``when`` field of an invariant to a tuple of phases."""
    if isinstance(raw, str):
        if raw == "both":
            return ("pre", "post")
        return (raw,)
    # array form — e.g. ["pre", "post"]
    return tuple(raw)


def _parse_input_field(raw: dict[str, Any]) -> InputField:
    """Parse an input field definition."""
    return InputField(
        type=raw["type"],
        required=raw.get("required", False),
        description=raw.get("description", ""),
        enum=tuple(raw["enum"]) if "enum" in raw else (),
        default=raw.get("default"),
    )


def _parse_notification(raw: dict[str, Any]) -> Notification:
    return Notification(
        channel=raw["channel"],
        message=raw.get("message", ""),
    )


def _parse_audit_effects(raw: dict[str, Any]) -> AuditEffects:
    return AuditEffects(
        record_prompt_hash=raw.get("record_prompt_hash", False),
        record_model=raw.get("record_model", False),
        record_response_ids=raw.get("record_response_ids", False),
        record_agent_id=raw.get("record_agent_id", False),
    )


def _parse_effects(raw: dict[str, Any] | None) -> Effects | None:
    if raw is None:
        return None
    return Effects(
        add_tags=tuple(raw.get("add_tags", [])),
        remove_tags=tuple(raw.get("remove_tags", [])),
        set_assignee=raw.get("set_assignee"),
        set_needs_producer_review=raw.get("set_needs_producer_review"),
        notifications=tuple(
            _parse_notification(n) for n in raw.get("notifications", [])
        ),
        audit=_parse_audit_effects(raw["audit"]) if "audit" in raw else None,
    )


def _parse_invariant(raw: dict[str, Any]) -> Invariant:
    return Invariant(
        name=raw["name"],
        description=raw["description"],
        when=_parse_when(raw["when"]),
        logic=raw.get("logic", ""),
    )


def _parse_command(name: str, raw: dict[str, Any]) -> Command:
    inputs: dict[str, InputField] = {}
    for input_name, input_raw in raw.get("inputs", {}).items():
        inputs[input_name] = _parse_input_field(input_raw)

    return Command(
        name=name,
        description=raw["description"],
        from_states=tuple(_parse_state_ref(s) for s in raw["from"]),
        to=_parse_state_ref(raw["to"]),
        actor=raw["actor"],
        pre=tuple(raw.get("pre", [])),
        post=tuple(raw.get("post", [])),
        inputs=inputs,
        prompt_ref=raw.get("prompt_ref", ""),
        effects=_parse_effects(raw.get("effects")),
        dispatch_map=dict(raw.get("dispatch_map", {})),
    )


def _parse_metadata(raw: dict[str, Any]) -> Metadata:
    return Metadata(
        name=raw["name"],
        description=raw["description"],
        owner=raw["owner"],
        roles=tuple(_parse_role(r) for r in raw["roles"]),
        links=dict(raw.get("links", {})),
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate_against_schema(
    descriptor_data: dict[str, Any],
    schema_path: Path | str | None,
) -> None:
    """Validate *descriptor_data* against the JSON Schema.

    If *schema_path* is ``None``, the default schema at
    ``docs/workflow/workflow-schema.json`` (relative to the repository root)
    is used.

    Raises ``DescriptorValidationError`` on failure.
    """
    if schema_path is None:
        # Walk up from this file to find the repo root (contains pyproject.toml)
        candidate = Path(__file__).resolve().parent
        while candidate != candidate.parent:
            if (candidate / "pyproject.toml").exists():
                break
            candidate = candidate.parent
        schema_path = candidate / "docs" / "workflow" / "workflow-schema.json"
    else:
        schema_path = Path(schema_path)

    if not schema_path.exists():
        raise DescriptorValidationError([f"Schema file not found: {schema_path}"])

    with open(schema_path) as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(descriptor_data), key=lambda e: list(e.path)
    ):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"[{path}] {error.message}")

    if errors:
        raise DescriptorValidationError(errors)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_descriptor(
    path: str | Path,
    *,
    schema_path: str | Path | None = None,
) -> WorkflowDescriptor:
    """Load a workflow descriptor from a YAML or JSON file.

    Parameters
    ----------
    path:
        Path to the workflow descriptor file (``.yaml``, ``.yml``, or ``.json``).
    schema_path:
        Optional path to the JSON Schema file.  When ``None`` the default
        schema at ``docs/workflow/workflow-schema.json`` is used.

    Returns
    -------
    WorkflowDescriptor
        Fully parsed, validated, immutable descriptor model.

    Raises
    ------
    DescriptorValidationError
        If the descriptor fails schema validation.
    FileNotFoundError
        If *path* does not exist.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Descriptor file not found: {file_path}")

    with open(file_path) as f:
        suffix = file_path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            data: dict[str, Any] = yaml.safe_load(f)
        elif suffix == ".json":
            data = json.load(f)
        else:
            raise ValueError(
                f"Unsupported descriptor format '{suffix}'. Use .yaml, .yml, or .json."
            )

    # Schema validation
    _validate_against_schema(data, schema_path)

    # Parse into typed model
    metadata = _parse_metadata(data["metadata"])

    states: dict[str, StateTuple] = {}
    for alias, st in data.get("states", {}).items():
        states[alias] = StateTuple(status=st["status"], stage=st["stage"])

    invariants = tuple(_parse_invariant(inv) for inv in data.get("invariants", []))

    commands: dict[str, Command] = {}
    for cmd_name, cmd_raw in data.get("commands", {}).items():
        commands[cmd_name] = _parse_command(cmd_name, cmd_raw)

    return WorkflowDescriptor(
        version=data["version"],
        metadata=metadata,
        statuses=tuple(data["status"]),
        stages=tuple(data["stage"]),
        states=states,
        terminal_states=tuple(data.get("terminal_states", [])),
        invariants=invariants,
        commands=commands,
    )
