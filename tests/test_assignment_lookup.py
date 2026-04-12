import pytest

from ampa.assignment import lookup_assignee


@pytest.mark.parametrize(
    "state,stage,expected",
    [
        ("open", "plan_complete", "Patch"),
        ("in-progress", "plan_complete", "Patch"),
        ("open", "intake_complete", "Archie"),
        ("in-progress", "intake_complete", "Archie"),
        ("open", "idea", "Map"),
        ("open", "in_review", "Patch"),
        ("open", "done", "Build"),
        ("blocked", "idea", "Build"),
        ("completed", "done", "Build"),
        # Unknown combos fall back to default
        ("open", "unknown_stage", "Build"),
        ("some_state", "some_stage", "Build"),
    ],
)
def test_lookup_assignee(state, stage, expected):
    assert lookup_assignee(state, stage) == expected
