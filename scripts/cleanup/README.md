# Cleanup Scripts

This folder contains non-interactive routines that mirror the `cleanup` skill
with explicit flags for dry runs, confirmations, and JSON reporting.

## Scripts

- `scripts/cleanup/prune_local_branches.py`
  - Prunes local branches that are merged into the default branch.
- `scripts/cleanup/cleanup_stale_remote_branches.py`
  - Lists (and optionally deletes) remote branches older than N days.

## Common flags

- `--dry-run`: do not make changes.
- `--yes`: assume yes for prompts.
- `--report <path>`: write JSON report to file.
- `--verbose`: increase logging (repeat for more detail).
- `--quiet`: suppress JSON output to stdout.

## Examples

Dry-run local branch cleanup:

```bash
python scripts/cleanup/prune_local_branches.py --dry-run \
  --branches-file /tmp/cleanup/branches_to_delete.json \
  --report /tmp/cleanup/local.json
```

Dry-run stale remote cleanup (90 days):

```bash
python scripts/cleanup/cleanup_stale_remote_branches.py --days 90 --dry-run --report /tmp/cleanup/remote.json
```


## Behavior notes vs cleanup skill

- These scripts are non-interactive by default, controlled via `--dry-run` and `--yes`.
- The cleanup skill includes conversational prompts and optional PR summaries; these
  scripts focus on deterministic JSON reporting and safe execution.
- Default branch detection and conservative merge checks follow the same approach
  described in `skill/cleanup/SKILL.md`.

## Requirements

- `git` for branch operations.
