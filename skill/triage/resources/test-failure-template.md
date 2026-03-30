## Failure Signature

- Test name: <test-name>
- Failing commit: <commit-hash> (if available)
- CI job: <ci-job-url> (if available)

## Evidence

- Short stderr/stdout excerpt (first 1k characters):

```
<paste excerpt here>
```

Attach larger logs as links rather than inline when necessary.

## Steps To Reproduce

1. Checkout the commit: `git checkout <commit-hash>`
2. Run the failing test: `pytest -k "<test-name>" -q` (or equivalent command)
3. Capture full logs and attach to the work item

## Impact

Describe the user or CI impact (e.g., "blocks all PR merges", "affects scheduler tests")

## Suggested Triage Steps

1. Verify flakiness: rerun CI/test locally once.
2. If reproducible, add owner from owner-inference heuristics and assign for triage.
3. If flaky, tag `flaky` and route to flaky-test queue.

## Links

- Runbook: skill/triage/resources/runbook-test-failure.md
- CI artifacts: <url>
