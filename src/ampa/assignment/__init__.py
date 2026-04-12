"""Assignment lookup utilities for deterministic State×Stage -> assignee mapping.

This module provides a simple loader and lookup function that reads
the static YAML mapping and returns the deterministic assignee for a
given (state, stage) pair. No availability/capacity checks are performed.
"""
from pathlib import Path
import json
from ampa.engine_factory import find_workflow_descriptor


def _load_mapping_from_descriptor():
    """Load the assignment_map object from the canonical workflow descriptor.

    The engine provides search precedence for workflow.json (project-local, XDG, packaged docs).
    We defer to the same lookup so the mapping lives with the canonical workflow descriptor.
    """
    descriptor_path = find_workflow_descriptor()
    if not descriptor_path:
        return {"default_assignee": "Build", "mappings": []}
    with open(descriptor_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("assignment_map", {"default_assignee": "Build", "mappings": []})


_MAPPING = _load_mapping_from_descriptor()


def lookup_assignee(state: str, stage: str) -> str:
    """Return deterministic assignee for given state and stage using workflow.json assignment_map.

    See mapping semantics in docs/workflow/workflow.json: assignment_map
    """
    state = state or ""
    stage = stage or ""
    mappings = _MAPPING.get("mappings", [])
    for m in mappings:
        states = m.get("states", [])
        mstage = m.get("stage")
        if state in states and (mstage == stage or mstage == "*"):
            return m.get("assignee")
    return _MAPPING.get("default_assignee")
