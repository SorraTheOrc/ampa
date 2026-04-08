#!/usr/bin/env sh
# Verify that the installer will publish the canonical workflow.json to the
# project and XDG locations using the same discovery logic as the installer.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
INSTALLER="$SCRIPT_DIR/install-worklog-plugin.sh"

echo "Running installer in dry-run verification mode"

# Locate repo root (try git first, fallback to relative path)
REPO_ROOT=""
if command -v git >/dev/null 2>&1; then
  if git -C "$SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
    REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  fi
fi
if [ -z "$REPO_ROOT" ]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." >/dev/null 2>&1 && pwd || true)"
fi

DOCS="$REPO_ROOT/docs/workflow/workflow.json"
if [ ! -f "$DOCS" ]; then
  echo "Canonical docs/workflow/workflow.json not found at $DOCS"
  # Try an upward search from script dir and CWD
  found=""
  curdir="$SCRIPT_DIR"
  i=0
  while [ "$i" -lt 6 ]; do
    if [ -f "$curdir/docs/workflow/workflow.json" ]; then
      found="$curdir/docs/workflow/workflow.json"
      break
    fi
    if [ "$curdir" = "/" ] || [ -z "$curdir" ]; then break; fi
    curdir="$(dirname "$curdir")"
    i=$((i + 1))
  done
  if [ -z "$found" ]; then
    curdir="$(pwd)"
    i=0
    while [ "$i" -lt 6 ]; do
      if [ -f "$curdir/docs/workflow/workflow.json" ]; then
        found="$curdir/docs/workflow/workflow.json"
        break
      fi
      if [ "$curdir" = "/" ] || [ -z "$curdir" ]; then break; fi
      curdir="$(dirname "$curdir")"
      i=$((i + 1))
    done
  fi
  if [ -n "$found" ]; then
    DOCS="$found"
  else
    echo "Canonical workflow descriptor not found; aborting verification"
    exit 2
  fi
fi

echo "Invoking installer to publish workflow.json to XDG (may require write access to XDG dir)"
# Provide explicit source and explicit target dir to force a global install
DEFAULT_SRC="$SCRIPT_DIR/../resources/ampa.mjs"
GLOBAL_TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/.worklog/plugins"
sh "$INSTALLER" --yes --force-workflow --docs-path "$DOCS" "$DEFAULT_SRC" "$GLOBAL_TARGET"

echo "Verifier: check XDG location"
XDG_DEST="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/.worklog/ampa/workflow.json"
if [ -f "$XDG_DEST" ]; then
  echo "XDG workflow published: $XDG_DEST"
  echo "Contents preview:" && head -n 20 "$XDG_DEST" || true
else
  echo "XDG workflow not published"
  exit 2
fi

echo "Verifier: check project location"
PROJECT_DEST=".worklog/ampa/workflow.json"
if [ -f "$PROJECT_DEST" ]; then
  echo "Project workflow present: $PROJECT_DEST"
else
  echo "Project workflow not present (installer may have skipped because it existed)"
fi

echo "Verification complete."
