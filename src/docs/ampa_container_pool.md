# AMPA Container Pool

This document explains the AMPA dev container pool used by `wl ampa start-work` and related commands.

Overview
- The AMPA container pool is a set of pre-warmed, identical dev containers (distrobox on top of Podman) kept ready so that `wl ampa start-work <work-item-id>` can claim a container instantly instead of doing a slow clone/build.
- The pool is managed per host and shared across projects; pool state and cleanup logs are stored under your XDG config directory (default `~/.config/opencode/.worklog/ampa/`).

Key concepts
- Template container: a single template (`ampa-template`) created once from the image (`ampa-dev:latest`) and used as the seed for pool clones.
- Pool containers: named `ampa-pool-0`, `ampa-pool-1`, … up to the configured pool max. These are lightweight clones of the template kept stopped and ready to be claimed.
- Claimed container: when you run `wl ampa start-work <id>` the command claims one pool container, records the claim in `pool-state.json`, and sets up the repository inside the container for the provided work item.
- Replenishment: after a claimed container is consumed `wl ampa` triggers a background replenishment to replace the used pool slot (or `wl ampa warm-pool` can be used interactively to pre-fill the pool).

Where state is stored
- Global pool state and cleanup logs: `~/.config/opencode/.worklog/ampa/` (see `pool-state.json` and `pool-cleanup.json`). This is computed by the plugin helper `globalAmpaDir()` in `skill/install-ampa/resources/ampa.mjs`.
- Per-project AMPA config/state: `<projectRoot>/.worklog/ampa/` (scheduler store, per-project `.env`, daemon PID/logs).

Commands
- `wl ampa warm-pool` — build image (if needed), create template, and fill the pool to the configured size. Run this once after install to avoid slow first-run latency. See `README.md` for the short how-to and `skill/install-ampa/resources/ampa.mjs` for implementation details.
- `wl ampa start-work <work-item-id>` — claim a pool container (fast) or fall back to cloning the template/direct repo clone; sets up `/workdir/project`, checks out the work branch, installs the `ampa` plugin inside the container, and drops you into an interactive shell. Use `--agent` to set the work item assignee.
- `wl ampa finish-work [<work-item-id>] --discard|--no-push` — finalize and destroy the claimed container. `--discard` (destructive) discards uncommitted changes; `--no-push` commits locally and syncs worklog but does not push to origin.
- `wl ampa list-containers` / `wl ampa lc` — list AMPA-created containers and claimed mapping.

Lifecycle
1. Call `wl ampa warm-pool` (recommended once after installation).
2. Run `wl ampa start-work <id>` — a pool container is claimed and prepared; you are placed in `/workdir/project` inside a shell with `AMPA_*` environment variables and helpers configured.
3. Do development interactively inside the container (build, test, commit).
4. Run `wl ampa finish-work` to commit/push/update the work item and destroy the container (or `--discard` to skip commit/push and destroy, losing uncommitted changes).
5. AMPA attempts to replenish the used pool slot in background.

Inspecting and debugging
- Pool state: open `~/.config/opencode/.worklog/ampa/pool-state.json` to see which pool containers are claimed and their mapping to work items.
- Logs: `~/.config/opencode/.worklog/ampa/pool-replenish.log` and per-project `.worklog/ampa/*.log` capture background replenish and plugin activity.
- List containers: `wl ampa list-containers --json` or use `podman ps -a --filter name=ampa-` to inspect raw containers.

Best practices
- Always run `wl ampa warm-pool` after installing or when the `ampa/Containerfile` changes.
- Prefer `wl ampa start-work` / `wl ampa finish-work` over manual `podman`/`distrobox` commands — the plugin manages pool state and cleanup.
- Use `--no-push` when you want to preserve local commits but cannot push from the container/network; use `--discard` only when you deliberately want to lose uncommitted changes.

Browser test support
- The AMPA container image may include Playwright/Chromium runtime dependencies so that browser tests can run inside claimed containers. When present the image and pinned Playwright version will be documented in the Containerfile and noted here.

Running browser tests inside a claimed container
- Ensure the image includes the pinned Playwright-compatible browser runtime (see the comment in `ampa/Containerfile` for the pinned version).
- To run the browser smoke test non-interactively from the host against a claimed container, run:

  ```sh
  CONTAINER=$(wl ampa list-containers --json | jq -r '.containers[0].name')
  distrobox enter "$CONTAINER" -- bash --login -c '. /etc/ampa_bashrc && cd /workdir/project && npm ci && npm run test:smoke:node'
  ```

- When recording validation evidence, capture the exact representative branch used, the full command run, and the command exit code. Post that information as a comment on the parent validation work item (for example: `SA-0MN0VR4QU180IKU7`).

Implementation reference
- Core pool logic and helpers live in: `skill/install-ampa/resources/ampa.mjs` (functions: `poolStatePath`, `claimPoolContainer`, `replenishPool`, `replenishPoolBackground`, `startWork`, `finishWork`).

Troubleshooting
- If `wl ampa start-work` fails due to missing tools, ensure `podman`, `distrobox`, `git`, and `wl` are installed and in PATH on the host.
- If Podman reports runtime errors about `/run/user/<uid>`, create the runtime directory (see top-level `README.md` for instructions).
- If a container cannot be removed from inside itself it will be marked for host-side cleanup and removed on the next pool operation.

Contact / contribution
- To change pool behavior or pool size edit `skill/install-ampa/resources/ampa.mjs` and update tests in `tests/node/test-ampa-devcontainer.mjs`.

Running commands inside a claimed container
-----------------------------------------

This section shows how to run commands inside a claimed AMPA container once `wl ampa start-work` has prepared it. You can run commands interactively (inside the shell the command drops you into) or execute single commands from the host (recommended for automation). Always ensure the AMPA environment is loaded (the setup writes `/etc/ampa_bashrc`) and `cd /workdir/project` so commands run in the project checkout.

Manual (interactive)

- Start and enter a container for the work item:

  ```sh
  wl ampa start-work WL-123
  # (the command sets up the container and drops you into an interactive shell)
  # inside the container you are at: /workdir/project
  ```

- Do work interactively (build, test, run):

  ```sh
  # inside the container
  npm ci
  npm test
  git add -A && git commit -m "WL-123: fix tests"
  ```

- Finish and release the container when done:

  ```sh
  # from inside the container or from host: wl ampa finish-work
  wl ampa finish-work
  # use --no-push to avoid pushing commits (commits are still created locally)
  wl ampa finish-work --no-push
  # use --discard to destroy the container and discard uncommitted changes
  wl ampa finish-work --discard
  ```

From host (single command)

Use `distrobox enter` (preferred) or `podman exec` to run a single non‑interactive command inside a claimed AMPA container. Always source `/etc/ampa_bashrc` to get the same env the interactive shell has.

- Find the claimed container name (host):

  ```sh
  # human readable
  wl ampa list-containers

  # machine readable — returns JSON with mapping to work items
  wl ampa list-containers --json
  ```

- Run a single command via distrobox:

  ```sh
  CONTAINER=ampa-pool-0   # replace with the claimed container name
  distrobox enter "$CONTAINER" -- bash --login -c 'cd /workdir/project && . /etc/ampa_bashrc && npm test'
  ```

- Or via podman (if you prefer):

  ```sh
  podman exec -w /workdir/project "$CONTAINER" bash -lc '. /etc/ampa_bashrc; npm test'
  ```

Programmatic (bash script)

Create small automation scripts that call `distrobox enter ... -- bash -lc` and capture exit codes and output. Example:

```sh
#!/usr/bin/env bash
set -euo pipefail
CONTAINER=ampa-pool-0
CMD='cd /workdir/project && . /etc/ampa_bashrc && npm ci && npm test'
distrobox enter "$CONTAINER" -- bash --login -c "$CMD"
EXIT=$?
echo "command exit: $EXIT"
exit $EXIT
```

Programmatic (Node example)

Use Node's `child_process.spawnSync` / `execFileSync` to run the same `distrobox` command from code. This mirrors how the AMPA plugin runs commands.

```js
import { spawnSync } from 'child_process';

const container = 'ampa-pool-0';
const cmd = 'cd /workdir/project && . /etc/ampa_bashrc && npm test';
const res = spawnSync('distrobox', ['enter', container, '--', 'bash', '--login', '-c', cmd], { encoding: 'utf8' });
console.log('status', res.status);
console.log('stdout', res.stdout);
console.error('stderr', res.stderr);
process.exit(res.status || 0);
```

Complete end-to-end example
---------------------------

Below is a complete sequence (host-side) showing: determine a work item, start the container, run a command inside it non‑interactively, and release the container.

```sh
# 1) Determine the canonical work item id (example using a PR title)
PR=123
WORK_ITEM=$(gh pr view "$PR" --json title --jq '.title' | grep -oE 'SA-[0-9]+' | head -n1)
echo "Work item: $WORK_ITEM"

# 2) Start and claim a container for the work item (drops into shell if interactive)
wl ampa start-work "$WORK_ITEM" &
# If you prefer to run programmatically without interactive attach, claim then find container:
# wl ampa start-work "$WORK_ITEM" --agent probe

# 3) Find the claimed container name
CONTAINER=$(wl ampa list-containers --json | jq -r --arg id "$WORK_ITEM" '.containers[] | select(.workItemId==$id) | .name')
echo "Claimed container: $CONTAINER"

# 4) Run command inside the container (non-interactive)
distrobox enter "$CONTAINER" -- bash --login -c 'cd /workdir/project && . /etc/ampa_bashrc && npm ci && npm test'

# 5) When done, release and destroy the container (commit/push by default)
wl ampa finish-work "$WORK_ITEM"

# Alternate: preserve commits but don't push
# wl ampa finish-work "$WORK_ITEM" --no-push

# Alternate destructive: discard uncommitted changes and destroy
# wl ampa finish-work "$WORK_ITEM" --discard
```

Notes
- `distrobox enter ... -- bash --login -c` is the same pattern used in the plugin for host→container scripted steps and is the safest way to preserve user mappings and environment.
- For automation that needs to run many short commands prefer bundling them into a single script and executing that script inside the container to avoid repeated distrobox/process startup overhead.
- Always prefer `wl ampa finish-work` to manually removing containers so pool state and cleanup logs remain consistent.

Browser test support
-------------------

This repository's AMPA dev container image now includes pinned Playwright and the Chromium browser runtime so that browser smoke tests can be executed inside claimed AMPA containers.

- Playwright is pinned via `ARG PLAYWRIGHT_VERSION` in `ampa/Containerfile` (see the Containerfile comment for the exact tag used).
- The image includes the system libraries required by Chromium to run headless in typical CI/Dev environments.
- To run the Node.js browser smoke test:

  ```sh
  # inside a claimed container (or a container started from ampa-dev:latest)
  cd /workdir/project
  npm ci --include=dev
  node --test tests/node/test-browser-smoke.mjs
  ```

If the test fails on first run, ensure dev dependencies are installed (`npm ci` or `npm install`) before re-running; the image includes the browser runtime but not your repository's node_modules which are mounted from the workspace.
