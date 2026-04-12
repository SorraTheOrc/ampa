"""Assignment lookup utilities for deterministic State×Stage -> assignee mapping.

This module provides a simple loader and lookup function that reads
the static YAML mapping and returns the deterministic assignee for a
given (state, stage) pair. No availability/capacity checks are performed.
"""
from pathlib import Path
import yaml

_MAPPING_PATH = Path(__file__).parent / "mapping.yaml"


def _load_mapping():
    with _MAPPING_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_MAPPING = _load_mapping()


def lookup_assignee(state: str, stage: str) -> str:
    """Return deterministic assignee for given state and stage.

    Matching rules:
    - Iterate mappings in order. If the provided state is in mapping.states
      and (mapping.stage == stage or mapping.stage == "*"), return mapping.assignee.
    - Otherwise return default_assignee from the YAML.

    Args:
        state: work item status/state (e.g., 'open', 'in-progress', 'blocked')
        stage: workflow stage (e.g., 'idea', 'intake_complete')
    Returns:
        Assignee name as configured (string).
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
