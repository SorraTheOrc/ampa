"""AMPA wrapper for GitHub sync commands.

Invoked by the scheduler as:
    python -m ampa.run_gh_sync import
    python -m ampa.run_gh_sync push

Responsibilities:
 1. Auto-detect the GitHub repo from ``git remote get-url origin`` when the
    worklog config does not have ``githubRepo`` set (or it is ``"(not set)"``).
 2. Run ``wl github import --create-new`` (import mode) or
    ``wl github push`` (push mode) as a subprocess.
 3. Exit 0 on success, non-zero on failure.  The scheduler's existing
    Discord notification integration handles failure alerts.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

_MODES = {"import", "push"}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_CONFIG_REL = os.path.join(".worklog", "config.yaml")


def _config_path() -> Path:
    """Return the absolute path to the worklog config file."""
    return Path(os.getcwd()) / _CONFIG_REL


def _read_config(path: Path) -> dict:
    """Read and return the worklog config as a dict.

    Uses simple ``key: value`` line parsing — no PyYAML dependency required.
    The worklog config is a flat mapping (no nesting) so this is sufficient.
    """
    if not path.is_file():
        return {}
    data: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Split on first colon only
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            # Handle YAML-style booleans and unquoted strings
            if value.lower() in ("true",):
                data[key] = True  # type: ignore[assignment]
            elif value.lower() in ("false",):
                data[key] = False  # type: ignore[assignment]
            elif value in ('""', "''"):
                data[key] = ""  # type: ignore[assignment]
            else:
                # Strip surrounding quotes if present
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                data[key] = value  # type: ignore[assignment]
    return data


def _write_config(path: Path, data: dict) -> None:
    """Write *data* back to the worklog config file.

    Produces simple ``key: value`` lines (one per entry).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _repo_from_config(cfg: dict) -> Optional[str]:
    """Return the configured GitHub repo or ``None`` if not set."""
    val = cfg.get("githubRepo")
    if not val or str(val).strip() in ("", "(not set)"):
        return None
    return str(val).strip()


# ---------------------------------------------------------------------------
# Git remote auto-detection
# ---------------------------------------------------------------------------

_GH_SSH_RE = re.compile(
    r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$"
)
_GH_HTTPS_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$"
)


def _detect_repo_from_remote() -> Optional[str]:
    """Parse the GitHub ``owner/repo`` from ``git remote get-url origin``."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            LOG.warning("git remote get-url origin failed: %s", result.stderr.strip())
            return None
        url = result.stdout.strip()
    except Exception:
        LOG.exception("Failed to run git remote get-url origin")
        return None

    for pattern in (_GH_SSH_RE, _GH_HTTPS_RE):
        m = pattern.match(url)
        if m:
            return f"{m.group('owner')}/{m.group('repo')}"
    LOG.warning("Could not parse GitHub owner/repo from remote URL: %s", url)
    return None


def ensure_repo_configured() -> Optional[str]:
    """Ensure ``githubRepo`` is set in worklog config; return the value.

    If the value is missing or ``"(not set)"``, attempt auto-detection from the
    git remote.  On success the config file is updated (idempotently).

    Returns ``None`` if the repo cannot be determined.
    """
    cfg_path = _config_path()
    cfg = _read_config(cfg_path)
    repo = _repo_from_config(cfg)
    if repo:
        return repo

    repo = _detect_repo_from_remote()
    if not repo:
        return None

    # Update config idempotently
    cfg["githubRepo"] = repo
    _write_config(cfg_path, cfg)
    LOG.info("Auto-detected GitHub repo %s from git remote; updated %s", repo, cfg_path)
    return repo


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def run_sync(mode: str) -> int:
    """Run the appropriate ``wl github`` command.

    Returns the process exit code (0 on success).
    """
    if mode not in _MODES:
        LOG.error("Unknown mode %r; expected one of %s", mode, sorted(_MODES))
        return 1

    repo = ensure_repo_configured()
    if not repo:
        LOG.error(
            "GitHub repo is not configured and could not be auto-detected. "
            "Set githubRepo in %s or ensure a GitHub git remote is configured.",
            _CONFIG_REL,
        )
        return 1

    if mode == "import":
        cmd = ["wl", "github", "import", "--create-new", "--repo", repo]
    else:
        cmd = ["wl", "github", "push", "--repo", repo]

    LOG.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=300)
        if result.returncode != 0:
            LOG.error("Command exited with code %d", result.returncode)
        return result.returncode
    except subprocess.TimeoutExpired:
        LOG.error("Command timed out after 300 seconds")
        return 1
    except Exception:
        LOG.exception("Failed to run command")
        return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) < 2 or sys.argv[1] not in _MODES:
        print(
            f"Usage: python -m ampa.run_gh_sync <{'|'.join(sorted(_MODES))}>",
            file=sys.stderr,
        )
        return 1

    return run_sync(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
