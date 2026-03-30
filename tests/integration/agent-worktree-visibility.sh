#!/usr/bin/env bash
set -euo pipefail

# Integration test: ensure agent-created worktree/branch is visible from another worktree
# Usage: ./tests/integration/agent-worktree-visibility.sh

ROOT_DIR=$(pwd)
SKILL=./skill/create-worktree-skill/scripts/run.sh
WORK_ITEM_ID=${1:-SA-0ML0502B21WHXDYA}
AGENT_A=testA

<<<<<<< HEAD
TMP_A_DIR=$(mktemp -d ".worktrees/tmp-worktree-${AGENT_A}-test-XXXXXX")
TMP_B_DIR=$(mktemp -d ".worktrees/tmp-worktree-testB-XXXXXX")

if [ -z "$TMP_A_DIR" ] || [ -z "$TMP_B_DIR" ]; then
  echo "Failed to create unique tmp dirs" >&2
  exit 1
fi
=======
TMP_A_DIR=".worktrees/tmp-worktree-${AGENT_A}-test"
TMP_B_DIR=".worktrees/tmp-worktree-testB"
>>>>>>> bb1b266 (Use .worktrees/ for test tmp worktree paths)

cleanup() {
  echo "Cleaning up..."
  set +e
  # prune stale worktree references first to clear broken gitdir entries
  git worktree prune >/dev/null 2>&1 || true
  # remove worktrees if present; be resilient to tracked/untracked changes and avoid noisy errors
  for p in "$TMP_A_DIR" "$TMP_B_DIR"; do
    if [ -z "$p" ]; then
      continue
    fi
    # If the directory exists on disk
    if [ -d "$p" ]; then
      # Check whether git still considers this path a registered worktree
      if git worktree list --porcelain | awk '/^worktree /{print substr($0,10)}' | grep -Fx "$p" >/dev/null 2>&1; then
        # Clean tracked/untracked files to allow a non-force remove
        git -C "$p" reset --hard >/dev/null 2>&1 || true
        git -C "$p" clean -fd >/dev/null 2>&1 || true
        # Attempt non-forced remove; fall back to forced only if necessary
        if ! git worktree remove "$p" >/dev/null 2>&1; then
          git worktree remove -f "$p" >/dev/null 2>&1 || true
        fi
      else
        # Not registered as a worktree; just delete the directory
        rm -rf "$p" >/dev/null 2>&1 || true
      fi
    else
      # Directory not present; ensure any stale registration is pruned
      git worktree prune >/dev/null 2>&1 || true
    fi
  done
  # final prune to clean any leftover refs
  git worktree prune >/dev/null 2>&1 || true
}

trap cleanup EXIT

echo "Running skill to create worktree and branch (Agent A)"
"$SKILL" "$WORK_ITEM_ID" "$AGENT_A"

# Determine branch name and commit hash
BRANCH="feature/${WORK_ITEM_ID}"
if ! git show-ref --verify --quiet refs/heads/${BRANCH}; then
  echo "Branch ${BRANCH} not found in repo refs" >&2
  exit 3
fi
COMMIT_A=$(git rev-parse ${BRANCH})
echo "Agent A created branch ${BRANCH} commit ${COMMIT_A}"

echo "Creating Agent B worktree"
# Avoid triggering post-pull hooks that run wl sync before we init the worktree
WORKLOG_SKIP_POST_PULL=1 git worktree add --checkout "$TMP_B_DIR" HEAD

pushd "$TMP_B_DIR" >/dev/null
echo "Agent B initializing worklog (non-interactive)"
# Provide defaults for non-interactive init (copy repo config and opencode.json) so wl init can complete
mkdir -p .worklog
if [ -f "${ROOT_DIR}/opencode.json" ]; then
  cp "${ROOT_DIR}/opencode.json" ./opencode.json || true
fi
if [ -f "${ROOT_DIR}/.worklog/config.yaml" ]; then
  cp "${ROOT_DIR}/.worklog/config.yaml" .worklog/config.yaml || true
fi
wl init --json > /tmp/wl_init_b_out 2>/tmp/wl_init_b_err || true
echo ".worklog after Agent B init:"; ls -la .worklog || true
echo "Agent B running wl sync"
wl sync
popd >/dev/null

echo "Verifying branch visibility from Agent B (repo-level)"
if ! git show-ref --verify --quiet refs/heads/${BRANCH}; then
  echo "Branch ${BRANCH} not visible after wl sync" >&2
  exit 4
fi
COMMIT_B=$(git rev-parse ${BRANCH})

echo "Compare commits: A=${COMMIT_A} B=${COMMIT_B}"
if [ "$COMMIT_A" != "$COMMIT_B" ]; then
  echo "Commit mismatch between worktrees" >&2
  exit 5
fi

echo "Integration test succeeded: branch ${BRANCH} is visible with matching commit ${COMMIT_A}"

exit 0
