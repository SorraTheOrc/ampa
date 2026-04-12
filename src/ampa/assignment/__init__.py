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

    # Try loading the resolved descriptor first. If it contains an
    # assignment_map, return it. Otherwise fall back to the packaged
    # docs/workflow/workflow.json (module-local canonical) which may
    # contain the mapping used by tests and defaults.
    try:
        with open(descriptor_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    assignment = data.get("assignment_map")
    if assignment:
        return assignment

    # fallback to repository-level canonical docs/workflow/workflow.json.
    # Ascend from this file to the repository root and look for docs/workflow/workflow.json.
    repo_root = Path(__file__).resolve()
    # climb up a few levels to find repo root (this file is at src/ampa/assignment)
    if len(repo_root.parents) >= 4:
        repo_root = repo_root.parents[3]
    else:
        repo_root = repo_root.parents[-1]
    module_docs = repo_root / "docs" / "workflow" / "workflow.json"
    if module_docs.is_file():
        try:
            with module_docs.open("r", encoding="utf-8") as f:
                module_data = json.load(f)
            return module_data.get("assignment_map", {"default_assignee": "Build", "mappings": []})
        except Exception:
            pass

    return {"default_assignee": "Build", "mappings": []}


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
