"""Fallback configuration helpers for interactive sessions."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional

VALID_MODES = {
    "hold",
    "auto-accept",
    "auto-decline",
    "accept-recommendation",
    "discuss-options",
}


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    return os.path.join(tempfile.gettempdir(), "opencode_tool_output")


def normalize_mode(value: Optional[str]) -> str:
    if not value:
        return "hold"
    raw = str(value).strip().lower()
    aliases = {
        "auto_accept": "auto-accept",
        "auto-accept": "auto-accept",
        "auto_decline": "auto-decline",
        "auto-decline": "auto-decline",
        "accept": "auto-accept",
        "decline": "auto-decline",
        "hold": "hold",
        "pause": "hold",
        "queue": "hold",
        # new modes
        "accept_recommendation": "accept-recommendation",
        "accept-recommendation": "accept-recommendation",
        "recommend": "accept-recommendation",
        "discuss_options": "discuss-options",
        "discuss-options": "discuss-options",
        "discuss": "discuss-options",
    }
    mode = aliases.get(raw, "hold")
    if mode not in VALID_MODES:
        return "hold"
    return mode


def config_path(tool_output_dir: Optional[str] = None) -> str:
    """Return the path to the fallback config file.

    Resolution order:
    1. ``AMPA_FALLBACK_CONFIG_FILE`` env var (explicit override, always wins).
    2. ``.worklog/ampa/fallback_config.json`` (new default location).
    3. ``<tool_output_dir>/ampa_fallback_config.json`` (legacy fallback — used
       only when the new-location file does not exist but the legacy one does).
    """
    override = os.getenv("AMPA_FALLBACK_CONFIG_FILE")
    if override:
        return override
    # New default: .worklog/ampa/fallback_config.json (project-local)
    new_default = os.path.join(".worklog", "ampa", "fallback_config.json")
    if os.path.exists(new_default):
        return new_default
    # Legacy fallback: check old location
    base = tool_output_dir or _tool_output_dir()
    legacy = os.path.join(base, "ampa_fallback_config.json")
    if os.path.exists(legacy):
        return legacy
    # Neither exists — return new default so new configs are created there
    return new_default


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"default": "hold", "projects": {}, "public_default": "hold"}
    if not isinstance(data, dict):
        return {"default": "hold", "projects": {}, "public_default": "hold"}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    default_mode = normalize_mode(data.get("default"))
    public_default = normalize_mode(data.get("public_default"))
    # Normalize projects. Support legacy mapping project->mode (string) and
    # enhanced mapping project-> { mode: <mode>, overrides: {<decision>: <mode>}}
    normalized_projects: Dict[str, Any] = {}
    for key, value in projects.items():
        if not key:
            continue
        project_key = str(key)
        if isinstance(value, dict):
            mode = normalize_mode(value.get("mode") or value.get("default"))
            overrides = {}
            raw_overrides = value.get("overrides")
            if isinstance(raw_overrides, dict):
                for dkey, dval in raw_overrides.items():
                    if not dkey:
                        continue
                    overrides[str(dkey)] = normalize_mode(dval)
            normalized_projects[project_key] = {"mode": mode, "overrides": overrides}
        else:
            # legacy string-style project entry
            normalized_projects[project_key] = normalize_mode(value)
    return {
        "default": default_mode,
        "projects": normalized_projects,
        "public_default": public_default,
    }


def save_config(config: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    path = path or config_path()
    default_mode = normalize_mode(config.get("default"))
    public_default = normalize_mode(config.get("public_default"))
    normalized = {
        "default": default_mode,
        "projects": {},
        "public_default": public_default,
    }
    projects = config.get("projects")
    if isinstance(projects, dict):
        for key, value in projects.items():
            if not key:
                continue
            pk = str(key)
            # Allow callers to pass either a string mode or a dict with mode/overrides
            if isinstance(value, dict):
                mode = normalize_mode(value.get("mode") or value.get("default"))
                overrides = {}
                raw_overrides = value.get("overrides")
                if isinstance(raw_overrides, dict):
                    for dkey, dval in raw_overrides.items():
                        if not dkey:
                            continue
                        overrides[str(dkey)] = normalize_mode(dval)
                # If there are overrides, write a dict; otherwise write a simple mode
                if overrides:
                    normalized["projects"][pk] = {"mode": mode, "overrides": overrides}
                else:
                    normalized["projects"][pk] = mode
            else:
                normalized["projects"][pk] = normalize_mode(value)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
    except Exception:
        pass
    return normalized


def resolve_mode(
    project_id: Optional[str],
    *,
    tool_output_dir: Optional[str] = None,
    env_override: bool = True,
    is_public: Optional[bool] = None,
    require_config: bool = False,
    decision: Optional[str] = None,
) -> str:
    if env_override:
        env_mode = os.getenv("AMPA_FALLBACK_MODE")
        if env_mode:
            return normalize_mode(env_mode)
    cfg_path = config_path(tool_output_dir)
    if require_config and not os.path.exists(cfg_path):
        return "auto-accept"
    cfg = load_config(cfg_path)
    projects = cfg.get("projects") or {}
    if project_id:
        project_key = str(project_id)
        if project_key in projects:
            proj_entry = projects.get(project_key)
            # If project entry is a mapping with overrides, check decision overrides first
            if isinstance(proj_entry, dict):
                overrides = proj_entry.get("overrides") or {}
                if decision and isinstance(overrides, dict) and decision in overrides:
                    return normalize_mode(overrides.get(decision))
                # fall back to the project's mode
                return normalize_mode(proj_entry.get("mode"))
            # legacy string entry
            if decision:
                # cannot have per-decision config in legacy entry, fall through
                return normalize_mode(proj_entry)
            return normalize_mode(proj_entry)
    if is_public is None:
        is_public = project_id is None
    if is_public:
        return normalize_mode(cfg.get("public_default"))
    return normalize_mode(cfg.get("default"))
