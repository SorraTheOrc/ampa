AMPA Core Heartbeat Sender

Run locally:

AMPA_DISCORD_BOT_TOKEN="your-bot-token" AMPA_DISCORD_CHANNEL_ID="your-channel-id" python -m ampa.daemon

## Separate test and CI channels

To prevent test noise from polluting the production/ops Discord channel, you can
route test notifications to a dedicated test channel using either:

1. **Per-message override** — pass `channel_id` to `notify()`:
   ```python
   from ampa.notifications import notify

   # Sends to a test channel instead of the configured default
   notify("Test", "Message", "test", channel_id=123456789012345678)
   ```

2. **Environment variable** — set `AMPA_DISCORD_TEST_CHANNEL_ID` in your `.env`:
   ```bash
   AMPA_DISCORD_TEST_CHANNEL_ID="123456789012345678"
   ```

The per-message override takes precedence over the environment variable.
If neither is set, messages go to `AMPA_DISCORD_CHANNEL_ID`.

## CI / dry-run mode

In CI pipelines, set `AMPA_DISABLE_DISCORD="1"` to skip actual Discord sends:

```yaml
env:
  AMPA_DISABLE_DISCORD: "1"
```

When set, `notify()` returns `True` immediately without sending to Discord.
The bot also respects this flag and skips sending in `_send_to_discord()`.

This lets you run tests that exercise the notification path without creating
Discord messages.

Scheduler

The scheduler runs periodic commands with a normalized-lateness algorithm and stores state on disk.

Run locally:

  python -m ampa.scheduler

Example scheduler store:

  python -m ampa.scheduler

Key config knobs (env):

- AMPA_SCHEDULER_STORE: path to the JSON scheduler store
- AMPA_SCHEDULER_POLL_INTERVAL_SECONDS: poll interval in seconds
- AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS: minimum gap between command starts
- AMPA_SCHEDULER_PRIORITY_WEIGHT: priority multiplier weight
- AMPA_LLM_HEALTHCHECK_URL: LLM availability probe URL
- AMPA_SCHEDULER_MAX_RUN_HISTORY: number of run history entries to keep
 - AMPA_VERIFY_PR_WITH_GH: when set to 1/true, enable verification of GitHub PR merge status
   via the `gh` CLI before auto-completing work items. Defaults to enabled when not set;
   per-command metadata `verify_pr_with_gh` can override this behavior.

See `ampa/scheduler_schema.md` for the command field schema, store layout, and tuning guidance.

Configuration via .env

When AMPA is installed globally (under `~/.config/opencode/.worklog/plugins/ampa_py/`),
each project gets its own isolated configuration and state.

**Per-project config directory:** `<projectRoot>/.worklog/ampa/`

The daemon resolves `.env` and `scheduler_store.json` relative to the project root
(the directory the daemon was started from). Resolution order:

**.env resolution (first file found wins):**
1. `<projectRoot>/.worklog/ampa/.env` — per-project config (recommended)
2. `<packageDir>/.env` — backward compat for single-project / local installs
3. `<projectRoot>/.env` — legacy repo-root fallback

**scheduler_store.json resolution:**
1. `AMPA_SCHEDULER_STORE` env var — explicit override (always takes precedence)
2. `<projectRoot>/.worklog/ampa/scheduler_store.json` — per-project state
3. `<packageDir>/scheduler_store.json` — backward compat (only if the file exists there)
4. Defaults to per-project path if no file exists yet

**Migrating from a global install (shared config):**

If you previously had a single `.env` and `scheduler_store.json` inside the
AMPA package directory (e.g. `~/.config/opencode/.worklog/plugins/ampa_py/ampa/`),
move them to each project that needs them:

```sh
# For each project that uses AMPA:
mkdir -p <projectRoot>/.worklog/ampa/

# Copy .env
cp ~/.config/opencode/.worklog/plugins/ampa_py/ampa/.env \
   <projectRoot>/.worklog/ampa/.env

# Copy scheduler_store.json (if it exists)
cp ~/.config/opencode/.worklog/plugins/ampa_py/ampa/scheduler_store.json \
   <projectRoot>/.worklog/ampa/scheduler_store.json
```

After migrating, each project can have independent bot tokens, scheduler state,
and other configuration. The old files in the package directory are still used as
a fallback if no per-project files are found, so existing single-project setups
continue to work without changes.

Example `<projectRoot>/.worklog/ampa/.env` contents:

AMPA_DISCORD_BOT_TOKEN="your-bot-token"
AMPA_DISCORD_CHANNEL_ID="your-channel-id"
AMPA_HEARTBEAT_MINUTES=1
AMPA_VERIFY_PR_WITH_GH=1

Environment variables are used if `.env` is not present. The daemon prefers
values from the `.env` file when available.

Installing dependencies (if running locally)

Add the runtime dependencies and install them in your environment:

```sh
 pip install -r ampa/requirements.txt
```

Developer-only container (for contributors)

The repository includes a developer Containerfile at `ampa/Containerfile` and
supporting files (`ampa/.containerignore`, `ampa/.env.sample`). These are
intended for development workflows (creating distrobox/podman dev images,
CI, and contributor convenience) and are not part of the operator-facing
documentation. Operator guidance for running AMPA is provided above and
focuses on the Python CLI (`python -m ampa.daemon` / `python -m ampa.scheduler`).

If you're contributing and need a dev container, inspect `ampa/Containerfile`
and the related files. Do not rely on container build/run instructions for
operator deployments; those were intentionally removed from user-facing docs.

Suggested next steps for contributors:

- Build a dev image locally via Podman or Docker using `ampa/Containerfile`.
- Use a distrobox or other dev container to reproduce CI environments.
- The Containerfile targets a minimal Debian slim base — adjust packages
  as needed for your distro or contributor workflow.

Note: CI and local development may use the Containerfile to run integration
tests; operator installations should continue to prefer the Python package
and the `wl ampa` commands described above.

Run as a daemon

The daemon defaults to sending a single heartbeat and exiting. To run the
scheduler loop under the daemon runtime you must explicitly enable it either
with the `--start-scheduler` flag or the `AMPA_RUN_SCHEDULER` environment
variable.

Examples:

  # Run daemon in the foreground and start the scheduler loop (recommended for testing)
  AMPA_DISCORD_BOT_TOKEN="your-bot-token" AMPA_DISCORD_CHANNEL_ID="your-channel-id" python -m ampa.daemon --start-scheduler

  # Enable scheduler via environment variable instead of the CLI flag
  AMPA_DISCORD_BOT_TOKEN="your-bot-token" AMPA_DISCORD_CHANNEL_ID="your-channel-id" AMPA_RUN_SCHEDULER=1 python -m ampa.daemon

  # Send a single heartbeat and exit
  AMPA_DISCORD_BOT_TOKEN="your-bot-token" AMPA_DISCORD_CHANNEL_ID="your-channel-id" python -m ampa.daemon --once

Notes:

- The scheduler uses the current working directory as the `command_cwd` for
  commands it runs, so start the daemon from the directory you want commands
  to execute in.
- Ensure `AMPA_DISCORD_BOT_TOKEN` and `AMPA_DISCORD_CHANNEL_ID` are set (or
  `ampa/.env` is present) before starting the daemon; missing bot token will
  cause the daemon to exit.
- Install runtime dependencies when running locally:

  pip install -r ampa/requirements.txt

Observability
-------------

AMPA exposes a lightweight observability surface intended for scraping by
Prometheus-compatible systems. The package provides a combined `/metrics` and
`/health` HTTP endpoint. By default these endpoints are served on port `8000`.

Environment variables:

- `AMPA_DISCORD_BOT_TOKEN` (required by the daemon): when unset or empty the
  `/health` endpoint returns `503 Service Unavailable` to indicate fatal
  misconfiguration.
- `AMPA_METRICS_PORT` (optional): port to serve `/metrics` and `/health` on
  (defaults to `8000`).

Metrics exported:

- `ampa_heartbeat_sent_total` (counter) — number of successful heartbeat sends
- `ampa_heartbeat_failure_total` (counter) — number of failed heartbeat sends
- `ampa_last_heartbeat_timestamp_seconds` (gauge) — epoch seconds of last
  successful heartbeat

Quick manual test

1. Install dev dependencies: `pip install -r ampa/requirements.txt`
2. Run the metrics server in Python REPL or a tiny script:

```python
from ampa.server import start_metrics_server
start_metrics_server(port=8000)
```

3. Verify health: `curl -sSf http://127.0.0.1:8000/health` (returns HTTP 200 when
   `AMPA_DISCORD_BOT_TOKEN` is set).
4. Verify metrics: `curl http://127.0.0.1:8000/metrics | grep ampa_heartbeat`

Integration test example (pytest)

The repository includes integration tests that start the server on an ephemeral
port. Run them with `pytest -q` and confirm `tests/test_metrics_and_health.py`
passes.

GitHub sync
-----------

The scheduler runs two commands to keep Worklog work items and GitHub Issues
in sync automatically:

- **gh-import** — runs `wl github import --create-new` every 30 minutes to
  pull new and updated GitHub Issues into the local worklog.
- **gh-push** — runs `wl github push` every 3 hours to mirror local work
  item changes back to GitHub Issues.

Both commands use the `ampa.run_gh_sync` wrapper module which handles:

- **Auto-detection of GitHub repo:** if `githubRepo` is not set in
  `.worklog/config.yaml` (or is `"(not set)"`), the wrapper parses
  `git remote get-url origin` (SSH and HTTPS formats) and writes the
  detected `owner/repo` value back to the config idempotently.
- **Error handling:** non-zero exit codes from `wl github` are propagated
  to the scheduler, which posts Discord alerts via the bot notification system.
- **Timeout:** commands are killed after 300 seconds.

Manual usage:

```sh
# Import GitHub Issues into worklog
python -m ampa.run_gh_sync import

# Push worklog changes to GitHub Issues
python -m ampa.run_gh_sync push
```

Scheduler admin CLI

  Use the scheduler CLI for admin tasks (listing, adding, updating commands):

    python -m ampa.scheduler list

  Run a command immediately by id:

    python -m ampa.scheduler run-once <command-id>

Live delegation

  Delegation runs as part of the audit cycle and only when `audit_only` is false.
  It also requires no in-progress work items. When idle, it selects the top
  `wl next` candidate and dispatches the appropriate workflow:

  - stage `idea`: runs `/intake <id>`
  - stage `intake_complete`: runs `/plan <id>`
  - stage `plan_complete`: runs `work on <id> using the implement skill`


Delegation report

Generate a report listing in-progress items, candidates from `wl next`,
and the top candidate with rationale. This command also runs idle delegation
when the system has no in-progress items. Set `audit_only` metadata to true
to skip dispatch.

  python -m ampa.scheduler delegation

Send the report to Discord (requires `AMPA_DISCORD_BOT_TOKEN`):

  AMPA_DISCORD_BOT_TOKEN="your-bot-token" AMPA_DISCORD_CHANNEL_ID="your-channel-id" python -m ampa.scheduler delegation --discord

Candidate selection

The candidate selection service calls `wl next --json` and returns the top
candidate from that response.

Conversation manager
--------------------

Use `ampa.conversation_manager` to start a conversation (record a pending prompt) or resume a session awaiting input.

Example:

```python
from ampa.conversation_manager import start_conversation, resume_session

# start: records a pending prompt and sets session state to `waiting_for_input`
meta = start_conversation("s-123", "Please confirm the change", {"work_item": "WL-1"})

# later, resume with a human response
res = resume_session("s-123", "yes")
print(res)
```

Pending prompt payload
----------------------

Pending prompts are persisted as JSON files named `pending_prompt_<session_id>_<stamp>.json`
under `AMPA_TOOL_OUTPUT_DIR` (defaults to a temp directory). These files include
the full prompt text, choices, and conversation context so responders can review
blocked sessions end-to-end.

Example payload:

```json
{
  "session": "s-123",
  "session_id": "s-123",
  "work_item": "WL-1",
  "summary": "Please confirm the change",
  "prompt_text": "Please confirm the change",
  "choices": ["yes", "no"],
  "context": [{"role": "user", "content": "ship it"}],
  "state": "waiting_for_input",
  "created_at": "2026-02-11T12:00:00Z",
  "stamp": "1739275200000"
}
```

Notifications and session metadata include `pending_prompt_file` (full path) so
operators can open the JSON directly.

SDK adapter notes
-----------------

Use the OpenCode Python SDK (`opencode-ai`) for direct integrations. This
module supports a lightweight adapter hook: pass a `sdk_client` object that
provides `start_conversation` and `resume_session` callables. The manager
invokes these hooks before updating the local `pending_prompt_*` and session
state files.

Example error handling:

```python
from ampa.conversation_manager import resume_session, NotFoundError, TimedOutError

try:
    resume_session("s-123", "yes")
except NotFoundError:
    print("No pending prompt found")
except TimedOutError:
    print("Pending prompt timed out")
```

References
----------

- Work item: SA-0MLHQU5IZ0PJIPVL
- Tests: `tests/test_conversation_manager.py`

Configuration
-------------

- `AMPA_TOOL_OUTPUT_DIR` — directory where pending prompt and session files are written (defaults to temp dir)
- `AMPA_RESUME_TIMEOUT_SECONDS` — resume timeout in seconds (default 86400 — 24h)
- `AMPA_FALLBACK_CONFIG_FILE` — path to JSON config for fallback behaviour (defaults to `.worklog/ampa/fallback_config.json`; falls back to legacy `$AMPA_TOOL_OUTPUT_DIR/ampa_fallback_config.json` if it exists)
- `AMPA_FALLBACK_MODE` — overrides all config values with a single fallback mode (`hold`, `auto-accept`, `auto-decline`, `accept-recommendation`, `discuss-options`)

Fallback config schema
----------------------

The fallback config file controls how AMPA responds when sessions need input. It
supports per-project overrides and a safe default for public projects (sessions
without a project id). If no config file exists and no env override is set, the
delegation scheduler keeps legacy behavior (auto-accept) until a config is saved.

Example (simple — one mode per project):

```json
{
  "default": "auto-accept",
  "public_default": "hold",
  "projects": {
    "internal-proj": "auto-accept",
    "sandbox": "auto-decline"
  }
}
```

Example (per-decision overrides):

```json
{
  "default": "hold",
  "public_default": "hold",
  "projects": {
    "internal-proj": {
      "mode": "auto-accept",
      "overrides": {
        "run-tests": "auto-accept",
        "open-pr": "discuss-options",
        "deploy": "hold"
      }
    }
  }
}
```

When a project entry is a string, it sets the mode for all decisions. When it is
an object, `mode` sets the project default and `overrides` maps decision
categories (freeform strings) to per-decision modes. Decision categories are not
validated — any string is accepted.

- `default`: fallback mode for non-public projects without an explicit override.
- `public_default`: fallback mode when `project_id` is missing (safe default for public).
- `projects`: map of `project_id` to a mode string **or** an object `{"mode": "<mode>", "overrides": {"<decision>": "<mode>"}}`.

Valid modes: `hold`, `auto-accept`, `auto-decline`, `accept-recommendation`, `discuss-options`.

Config file location
--------------------

The default config location is `.worklog/ampa/fallback_config.json`. For backward
compatibility, if a config file exists at the legacy location
(`$AMPA_TOOL_OUTPUT_DIR/ampa_fallback_config.json`) but not at the new location,
it will be used. The `AMPA_FALLBACK_CONFIG_FILE` env var overrides both defaults.

Mode behavior
-------------

- `hold`: always hold for human input.
- `auto-accept`: automatically respond with `accept` when a response is required.
- `auto-decline`: automatically respond with `decline` when a response is required.
- `accept-recommendation`: inspect the payload for a `recommendation` field. If present, auto-apply the recommended action (`accept` or `decline`). If absent, fall back to `hold`.
- `discuss-options`: intended for multi-turn conversation with the agent (not yet implemented). Currently falls back to `hold` with a log message.

Error Reporting
---------------

All CLI commands use a centralised error report helper (`ampa/error_report.py`) for
unhandled errors. When a command encounters an unexpected exception, a structured
Error Report is printed to stderr containing:

- Command name and arguments
- Error type and message
- Timestamp, hostname, Python version, and platform
- Full traceback (in verbose/human mode)
- Suggested exit code

The helper exposes three public functions:

```python
from ampa.error_report import build_error_report, render_error_report, render_error_report_json

try:
    do_work()
except Exception as exc:
    report = build_error_report(exc, command="my-cmd", args={"id": "foo"})
    render_error_report(report, file=sys.stderr, verbose=True)   # human-readable
    render_error_report_json(report, file=sys.stderr)            # JSON
```

When implementing new CLI commands, unhandled errors will automatically be
caught and rendered by the `main()` entry point. For command-internal error
paths (e.g. inside `_cli_run`), call the helper directly.


## Interactive Buttons (MVP)

The Discord bot supports interactive buttons via the component protocol extension.
Messages sent through the Unix socket protocol may include an optional `components`
list to attach `discord.ui.Button` elements to the message.

### Component protocol

Each component object must have `type`, `label`, and `custom_id` fields. `style` is
optional and defaults to `secondary`. Supported styles: `primary`, `secondary`,
`success`, `danger`.

Example socket message with buttons:

```json
{
  "content": "Pick a colour",
  "components": [
    {"type": "button", "label": "Blue", "style": "primary", "custom_id": "test_blue"},
    {"type": "button", "label": "Red",  "style": "danger",  "custom_id": "test_red"}
  ]
}
```

When `components` is absent or empty, messages are sent as plain text (backward-compatible).

### Notification API

The `notify()` function in `ampa.notifications` accepts an optional `components`
keyword argument:

```python
from ampa.notifications import notify

notify(
    title="Blue or Red?",
    body="Pick a colour by clicking a button below.",
    message_type="command",
    components=[
        {"type": "button", "label": "Blue", "style": "primary", "custom_id": "test_blue"},
        {"type": "button", "label": "Red", "style": "danger", "custom_id": "test_red"},
    ],
)
```

When `payload` is supplied to `notify()`, the `components` keyword argument is ignored
— include components directly in the payload dict instead.

### Interaction handling

Clicking a button triggers the `on_interaction` event in `discord_bot.py`. The handler:

1. Filters to component-type (button) interactions only.
2. Extracts `custom_id`, user identity, and UTC timestamp.
3. Routes through `_route_interaction()` — no-op for `test_*` prefixes (MVP).
4. Derives a human-readable label from the `custom_id` (e.g. `test_blue` -> `Blue`).
5. Sends an in-channel acknowledgement: "You selected Blue, good luck. (clicked by user#1234, 2026-01-01T00:00:00Z)".

### Scheduler interactive buttons

The scheduler previously included an interactive `test-button` command for
feature validation. That command and its auto-registration were removed —
operators who want an interactive test message may add a custom command to
their project-local `scheduler_store.json` and invoke `ampa.notifications.notify`
with a `components` payload (see the "Interactive Buttons (MVP)" section).

Project References
------------------

- PRD: `docs/prd/PRD_Automated_PM_Agent_-_end-to-end_project_overseer_(SA-0ML5E2A2V1YILTVR).md`
- Parent epic: SA-0ML5E2A2V1YILTVR (Automated PM Agent — end-to-end project overseer)
- Parent work item: SA-0ML7NOSHP0MJC6HS (AMPA Daemon: Discord heartbeat & notifier)
- Documentation work item: SA-0ML7OB1H71YWK6OG (Documentation & Operator Runbook)
