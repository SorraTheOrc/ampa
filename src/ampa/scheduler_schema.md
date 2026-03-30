# AMPA Scheduler Configuration & Store Schema

This document describes the scheduler command fields, config knobs, and the on-disk store layout used by `ampa.scheduler`.

## Command fields

Each scheduled command is represented by a `CommandSpec` entry.

- `id` (string, required): unique command identifier.
- `command` (string, required): shell command executed by the scheduler (except for `type: heartbeat`).
- `requires_llm` (boolean, required): true if the command requires an LLM server.
- `frequency_minutes` (int > 0, required): desired minimum interval between runs.
- `priority` (int, required): higher numbers increase scheduling weight.
- `metadata` (object, optional): arbitrary metadata for operators.
- `max_runtime_minutes` (int, optional): execution timeout in minutes.
 - `type` (string, optional): `shell` (default) or `heartbeat`. Heartbeat triggers the heartbeat sender without running a shell command. Other built-in types: `stale-delegation-watchdog` (auto-registered, runs every 30m). The `test-button` command is available but not auto-registered.

## Config knobs (env)

- `AMPA_SCHEDULER_STORE`: **deprecated** — the scheduler store is now always resolved to `<projectRoot>/.worklog/ampa/scheduler_store.json`.
- `AMPA_SCHEDULER_POLL_INTERVAL_SECONDS`: scheduler loop interval in seconds (default: 5).
- `AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS`: global minimum interval between command starts (default: 60).
- `AMPA_SCHEDULER_PRIORITY_WEIGHT`: priority multiplier weight (default: 0.1).
- `AMPA_LLM_HEALTHCHECK_URL`: HTTP endpoint for LLM availability (default: `http://localhost:8000/health`).
- `AMPA_SCHEDULER_MAX_RUN_HISTORY`: number of runs retained per command (default: 50).

Scheduled shell commands run in the working directory where the scheduler daemon was started.

## Delegation gating

Delegation is gated by `audit_only` metadata in the audit and delegation commands. When
`audit_only` is true, delegation is skipped. Otherwise it no-ops if any `wl in_progress` items
exist. When idle, it selects the top `wl next` candidate and dispatches the appropriate workflow
command.

## Store schema

The store is a JSON file with the following top-level structure (see `ampa/scheduler_store.json` for the default):

```
{
  "commands": {
    "cmd-1": {
      "id": "cmd-1",
      "command": "python -m ...",
      "requires_llm": false,
      "frequency_minutes": 10,
      "priority": 2,
      "metadata": {},
      "max_runtime_minutes": 5
    }
    ,
    "heartbeat": {
      "id": "heartbeat",
      "command": "",
      "requires_llm": false,
      "frequency_minutes": 1,
      "priority": 1,
      "metadata": {},
      "max_runtime_minutes": null,
      "type": "heartbeat"
    },
    "wl-in_progress": {
      "id": "wl-in_progress",
      "command": "wl in_progress",
      "requires_llm": false,
      "frequency_minutes": 1,
      "priority": 0,
      "metadata": {},
      "max_runtime_minutes": 1,
      "type": "shell"
    },
    "wl-status": {
      "id": "wl-status",
      "command": "wl status",
      "requires_llm": false,
      "frequency_minutes": 10,
      "priority": 0,
      "metadata": {},
      "max_runtime_minutes": 1,
      "type": "shell"
    },
    "gh-import": {
      "id": "gh-import",
      "command": "python -m ampa.run_gh_sync import",
      "requires_llm": false,
      "frequency_minutes": 30,
      "priority": 0,
      "metadata": { "discord_label": "gh import" },
      "max_runtime_minutes": 5,
      "type": "shell"
    },
    "gh-push": {
      "id": "gh-push",
      "command": "python -m ampa.run_gh_sync push",
      "requires_llm": false,
      "frequency_minutes": 180,
      "priority": 0,
      "metadata": { "discord_label": "gh push" },
      "max_runtime_minutes": 5,
      "type": "shell"
    },
    /* The test-button example is intentionally omitted; operators who want
       an interactive test message can add a `test-button` command to their
       local scheduler_store.json. */
  },
  "state": {
    "cmd-1": {
      "running": false,
      "last_start_ts": "2026-02-04T00:00:00+00:00",
      "last_run_ts": "2026-02-04T00:02:00+00:00",
      "last_duration_seconds": 120.0,
      "last_exit_code": 0,
      "run_history": [
        {
          "start_ts": "2026-02-04T00:00:00+00:00",
          "end_ts": "2026-02-04T00:02:00+00:00",
          "duration_seconds": 120.0,
          "exit_code": 0
        }
      ]
    }
  },
  "last_global_start_ts": "2026-02-04T00:00:00+00:00"
}
```

## Admin CLI

- `python -m ampa.scheduler list [--json]`: list configured scheduler commands.
- `python -m ampa.scheduler ls`: alias for `list`.
- `python -m ampa.scheduler run-once <command-id>`: execute a stored command immediately and
  return its exit code.


## Scheduling algorithm

The scheduler computes a normalized lateness per command and multiplies by a priority factor:

- `desired_interval = frequency_minutes * 60`
- `time_since_last = now - last_run` (or a very large value when never run)
- `lateness = time_since_last - desired_interval`
- `normalized_lateness = max(lateness / desired_interval, 0)`
- `priority_factor = 1 + (priority_weight * priority)`
- `score = normalized_lateness * priority_factor`

Commands with the highest score are selected. Negative lateness is clamped to zero, which prevents early runs. Over time, even low priority commands accumulate enough lateness to run, preventing starvation.

## Suggested tuning values

- `AMPA_SCHEDULER_POLL_INTERVAL_SECONDS=5`: fast scheduling cadence for near real-time scheduling.
- `AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS=60`: ensure only one command starts per minute to avoid overload.
- `AMPA_SCHEDULER_PRIORITY_WEIGHT=0.1`: priority range 0-10 maps to a multiplier 1.0-2.0.
- `AMPA_SCHEDULER_MAX_RUN_HISTORY=50`: keeps recent run metrics without unbounded growth.
