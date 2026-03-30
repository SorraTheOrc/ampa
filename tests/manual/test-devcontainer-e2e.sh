#!/usr/bin/env bash
# Manual end-to-end test for dev container lifecycle (start-work / finish-work).
#
# Prerequisites:
#   - podman installed and working
#   - distrobox installed
#   - wl CLI available in $PATH
#   - A valid work item ID to use for testing
#
# Usage:
#   ./tests/manual/test-devcontainer-e2e.sh <work-item-id>
#
# This script will:
#   1. Run wl ampa start-work <id> to create a container
#   2. Inside the container: make a trivial change and commit
#   3. Run wl ampa finish-work to push and clean up
#   4. Verify the branch was pushed, work item updated, container destroyed
#
# NOTE: This script requires interactive confirmation at certain steps.

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <work-item-id>"
  echo ""
  echo "Example: $0 SA-XXXXXXXXXXXX"
  exit 2
fi

WORK_ITEM_ID="$1"

echo "============================================"
echo " Dev Container E2E Test"
echo " Work Item: $WORK_ITEM_ID"
echo "============================================"
echo ""

# Step 0: Check prerequisites
echo "[Step 0] Checking prerequisites..."
for bin in podman distrobox git wl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: $bin is not installed. Please install it first."
    exit 1
  fi
  echo "  OK: $bin found"
done
echo ""

# Step 1: Verify the work item exists
echo "[Step 1] Verifying work item $WORK_ITEM_ID..."
if ! wl show "$WORK_ITEM_ID" --json >/dev/null 2>&1; then
  echo "ERROR: Work item $WORK_ITEM_ID not found."
  exit 1
fi
echo "  OK: Work item exists"
echo ""

# Step 2: Check no existing container
CONTAINER_NAME="ampa-$WORK_ITEM_ID"
echo "[Step 2] Checking for existing container $CONTAINER_NAME..."
if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
  echo "WARNING: Container $CONTAINER_NAME already exists."
  echo "  You may want to remove it first: distrobox rm --force $CONTAINER_NAME"
  echo "  Continuing anyway..."
fi
echo ""

# Step 3: Run start-work
echo "[Step 3] Running: wl ampa start-work $WORK_ITEM_ID"
echo "  This will create a container and enter it."
echo "  Once inside, run the following commands:"
echo ""
echo "    cd /workdir/project"
echo "    echo 'test change' >> test-devcontainer.txt"
echo "    git add test-devcontainer.txt"
echo "    git commit -m 'test: verify dev container workflow'"
echo "    wl ampa finish-work"
echo ""
echo "  Then exit the container (type 'exit')."
echo ""
echo "Press Enter to start..."
read -r

wl ampa start-work "$WORK_ITEM_ID"

echo ""
echo "[Step 4] Verifying cleanup..."

# Check container is gone
if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
  echo "WARNING: Container $CONTAINER_NAME still exists."
  echo "  Run: distrobox rm --force $CONTAINER_NAME"
else
  echo "  OK: Container $CONTAINER_NAME destroyed"
fi

# Check branch was pushed
WORK_ITEM_JSON=$(wl show "$WORK_ITEM_ID" --json 2>/dev/null || echo "{}")
echo ""
echo "  Work item state after finish-work:"
echo "$WORK_ITEM_JSON" | head -20
echo ""

echo "============================================"
echo " E2E test complete."
echo " Verify:"
echo "   1. Branch was pushed to origin"
echo "   2. Work item status updated"
echo "   3. Container destroyed"
echo "============================================"
