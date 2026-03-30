"""Unit tests for the owner-inference skill (infer_owner.py)."""

import json
import os
import subprocess
import tempfile

import skill.owner_inference.scripts.infer_owner as io


# ---------------------------------------------------------------------------
# Owner map tests
# ---------------------------------------------------------------------------


def test_owner_map_match(tmp_path):
    """Override map returns the configured owner with confidence 1.0."""
    triage_dir = tmp_path / ".opencode" / "triage"
    triage_dir.mkdir(parents=True)
    (triage_dir / "owner-map.yaml").write_text(
        "tests/test_scheduler*: scheduler-team\n"
    )
    result = io.check_owner_map(str(tmp_path), "tests/test_scheduler_run.py")
    assert result is not None
    assignee, confidence, reason = result
    assert assignee == "scheduler-team"
    assert confidence == 1.0
    assert "owner-map" in reason


def test_owner_map_no_match(tmp_path):
    """Override map returns None when no pattern matches."""
    triage_dir = tmp_path / ".opencode" / "triage"
    triage_dir.mkdir(parents=True)
    (triage_dir / "owner-map.yaml").write_text("ampa/*: ampa-team\n")
    result = io.check_owner_map(str(tmp_path), "tests/test_foo.py")
    assert result is None


def test_owner_map_missing_file(tmp_path):
    """Returns None when the owner-map.yaml file does not exist."""
    result = io.check_owner_map(str(tmp_path), "tests/test_foo.py")
    assert result is None


def test_owner_map_comments_and_blanks(tmp_path):
    """Comments and blank lines are ignored."""
    triage_dir = tmp_path / ".opencode" / "triage"
    triage_dir.mkdir(parents=True)
    (triage_dir / "owner-map.yaml").write_text(
        "# This is a comment\n\nskill/triage/*: triage-team\n"
    )
    result = io.check_owner_map(str(tmp_path), "skill/triage/SKILL.md")
    assert result is not None
    assert result[0] == "triage-team"


# ---------------------------------------------------------------------------
# CODEOWNERS tests
# ---------------------------------------------------------------------------


def test_codeowners_match(tmp_path):
    """CODEOWNERS file matches file path and returns the owner."""
    (tmp_path / "CODEOWNERS").write_text("*.py @dev-team\n")
    result = io.check_codeowners(str(tmp_path), "tests/test_foo.py")
    assert result is not None
    assignee, confidence, reason = result
    assert assignee == "dev-team"
    assert confidence == 0.8
    assert "CODEOWNERS" in reason


def test_codeowners_last_match_wins(tmp_path):
    """When multiple rules match, the last one wins (GitHub convention)."""
    (tmp_path / "CODEOWNERS").write_text("* @general-team\ntests/* @test-team\n")
    result = io.check_codeowners(str(tmp_path), "tests/test_foo.py")
    assert result is not None
    assert result[0] == "test-team"


def test_codeowners_missing(tmp_path):
    """Returns None when no CODEOWNERS file exists."""
    result = io.check_codeowners(str(tmp_path), "tests/test_foo.py")
    assert result is None


def test_codeowners_github_dir(tmp_path):
    """Finds CODEOWNERS in .github/ directory."""
    gh_dir = tmp_path / ".github"
    gh_dir.mkdir()
    (gh_dir / "CODEOWNERS").write_text("skill/* @skill-team\n")
    result = io.check_codeowners(str(tmp_path), "skill/triage/SKILL.md")
    assert result is not None
    assert result[0] == "skill-team"


# ---------------------------------------------------------------------------
# Git blame tests
# ---------------------------------------------------------------------------


def test_git_blame_returns_top_author(tmp_path):
    """Git blame returns the most frequent author."""
    # Set up a real git repo for blame to work
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "a@test.com"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Author A"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    test_file = tmp_path / "test_file.py"
    test_file.write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    result = io.check_git_blame(str(tmp_path), "test_file.py")
    assert result is not None
    assignee, confidence, reason = result
    assert assignee == "Author A"
    assert confidence > 0
    assert "git blame" in reason


def test_git_blame_missing_file(tmp_path):
    """Returns None when the file does not exist."""
    result = io.check_git_blame(str(tmp_path), "nonexistent.py")
    assert result is None


# ---------------------------------------------------------------------------
# Recent commits tests
# ---------------------------------------------------------------------------


def test_recent_commits_returns_top_committer(tmp_path):
    """Recent commits heuristic returns the most frequent committer."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "b@test.com"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Author B"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    test_file = tmp_path / "test_file.py"
    test_file.write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "c1"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    test_file.write_text("v2\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "c2"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    result = io.check_recent_commits(str(tmp_path), "test_file.py")
    assert result is not None
    assignee, confidence, reason = result
    assert assignee == "Author B"
    assert confidence > 0
    assert "recent commits" in reason


def test_recent_commits_no_history(tmp_path):
    """Returns None when git log has no commits for the file."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    result = io.check_recent_commits(str(tmp_path), "nonexistent.py")
    assert result is None


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


def test_fallback_when_no_heuristic_matches(tmp_path):
    """When no heuristic matches, fallback to Build with confidence 0.0."""
    result = io.infer_owner(str(tmp_path), "nonexistent.py")
    assert result["assignee"] == "Build"
    assert result["confidence"] == 0.0
    assert result["heuristic"] == "fallback"


# ---------------------------------------------------------------------------
# Integration: infer_owner with override map
# ---------------------------------------------------------------------------


def test_infer_owner_prefers_override_map(tmp_path):
    """Override map takes precedence over other heuristics."""
    # Set up git repo with blame data
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "c@test.com"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Author C"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    test_file = tmp_path / "test_file.py"
    test_file.write_text("code\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    # Add override map
    triage_dir = tmp_path / ".opencode" / "triage"
    triage_dir.mkdir(parents=True)
    (triage_dir / "owner-map.yaml").write_text("test_file*: override-owner\n")

    result = io.infer_owner(str(tmp_path), "test_file.py")
    assert result["assignee"] == "override-owner"
    assert result["confidence"] == 1.0
    assert result["heuristic"] == "owner_map"


def test_infer_owner_threshold(tmp_path):
    """High threshold causes low-confidence heuristics to be skipped."""
    result = io.infer_owner(str(tmp_path), "nonexistent.py", confidence_threshold=2.0)
    assert result["assignee"] == "Build"
    assert result["heuristic"] == "fallback"
