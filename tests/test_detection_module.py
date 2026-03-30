import sys
from pathlib import Path

# Ensure repository root is on PYTHONPATH for test discovery of `plan` package
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plan import detection


def test_choose_blocker_prefers_sortindex_then_createdat():
    items = [
        {"id": "A", "sortIndex": 100, "createdAt": "2026-01-02T00:00:00"},
        {"id": "B", "sortIndex": 100, "createdAt": "2026-01-01T00:00:00"},
        {"id": "C", "sortIndex": 50, "createdAt": "2026-01-01T00:00:00"},
    ]
    assert detection.choose_blocker(items) == "B"


def test_choose_blocker_sortindex_wins():
    items = [
        {"id": "A", "sortIndex": 200, "createdAt": "2026-01-05T00:00:00"},
        {"id": "B", "sortIndex": 100, "createdAt": "2026-01-01T00:00:00"},
    ]
    assert detection.choose_blocker(items) == "A"


def test_group_overlaps_groups_paths():
    children = [
        {"id": "a", "allowed_files": ["src/app.py", "README.md"]},
        {"id": "b", "allowed_files": ["src/app.py"]},
        {"id": "c", "allowed_files": []},
    ]
    groups = detection.group_overlaps(children)
    assert "src/app.py" in groups
    assert len(groups["src/app.py"]) == 2
    assert "README.md" in groups and len(groups["README.md"]) == 1
