---
description: <description of the command's purpose and primary-goal>
tags:
  - command
agent: <agent-name>
model: <model-override> # optional, e.g. anthropic/claude-3-5-sonnet-20241022
subtask: <true|false>   # optional: usually true, force subagent invocation when true, if you and your collaborators care about the outputs, but not the process then this should be true as it limits context size.
---

## Description

You are executing an agentic command to <primary-goal> — coordinate, plan, and take safe automated steps to achieve the user's intent while deferring human approval for risky or irreversible actions.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- Short 1–2 sentence headline summarizing the final outcome.
- Primary artifact(s): names and paths of files created or updated (e.g. `.opencode/tmp/<slug>-<id>.md`).
- Idempotence requirement: describe how reruns should behave (e.g. reuse existing resources, avoid duplicates).

## Behavior

Describe the command's canonical execution flow and responsibilities. Use numbered steps for the main process and short sub-bullets for required checks.

### Hard requirements

- Use an iterative, conversational style when user input is required; prefer short, high-signal questions.
- Never take irreversible actions without explicit confirmation from the user (unless `--auto-approve` is provided and documented).
- Do not invent facts or credentials; ask for missing values when necessary.
- Respect repository and system ignore rules (e.g. `.gitignore`, OpenCode ignore policies).
- Preserve author intent: where uncertain, ask a clarifying question rather than making an assumption.
- When recommending next steps, always make the first recommendation the immediate next procedural step and briefly describe it.
- Provide copy-paste ready shell commands when the agent asks the user to run CLI steps.

## Process (must follow)

1. Validate inputs (agent responsibility)

- Check `$1` for correct format; if invalid, ask a clarifying question and offer creation options.
- Parse `$ARGUMENTS` for a short seed intent and extract 2–6 keywords to guide repository searches.

Note: use `$ARGUMENTS` as the authoritative seed intent when no explicit id is provided.

2. Context discovery (agent responsibility)

- Use derived keywords to search the repo and related systems for relevant docs, work items, and code.
- Output clearly labeled lists:
  - "Potentially related files" (file paths)
  - "Potentially related work items" (titles and ids)
- Summarize relevant findings in 1–3 sentences each.

Prefer short lists and one-line summaries to keep prompts focused and performant.

3. Plan & safety checks (agent responsibility)

- Draft a concise proposed plan of automated steps (1–4 bullets) that the agent intends to run.
- Run a safety checklist: identify irreversible actions, required credentials, permissions, and potential risks.
- If any unsafe or permissioned actions are present, require explicit user confirmation before proceeding.

Document any required credentials or external permissions and do not assume they exist.

4. Execute incremental steps (agent responsibility; prefer small, reversible actions)

- When possible, run actions in dry-run mode (`--dry-run`) first and present results.
- Apply changes incrementally and produce artifacts under `.opencode/tmp/` with descriptive names.
- After each automated step, summarize what changed and output commands used (copy-paste ready).

Always show the exact commands the agent ran (or would run in dry-run) using fenced code blocks so the user can reproduce steps.

5. Human approval (required for nontrivial changes)

- Present final artifact(s) and a 1–2 sentence headline for user approval.
- Offer: approve and apply, request edits, or abort. If approved, proceed to finalization.

6. Finalize changes (agent responsibility)

- Make final updates to persistent artifacts (work items, files, PRs) idempotently.
- Run any sync commands required (e.g. `wl sync`) and show results.
- Clean up temporary files created in `.opencode/tmp/`.

If publishing changes to the repository, show the `git` commands used (e.g. `git add`, `git commit`) and do not push unless explicitly requested.

7. Completion

- Output: final artifact ids, one-line headline, and a short list of next recommended steps.
- End with a fixed closing line: "This completes the <command-name> process for <<work-item-id>>"

## Traceability & idempotence

- Describe how the command maintains traceability (e.g. include source seed, user id, and timestamp in artifacts).
- Ensure rerunning the command does not create duplicate resources; list the deduplication strategy.

Record provenance in any created artifacts (seed intent, user who ran the command, timestamp, command invocation string).

## Editing rules & safety

- Make minimal, conservative edits to user-provided content; surface clarifying questions for ambiguous intent.
- Do not include or quote content from files excluded by ignore rules.
- Avoid exposing secrets: never echo contents of files that match common secret patterns (e.g. `.env`, `credentials.json`) unless explicitly allowed.
- Respect OpenCode file-include rules: do not include binary files or very large files inline.
- If automated steps fail, stop and present a clear Open Question with suggested remedial actions.

## Example invocation

```
/<command-name> <<work-item-id>> optional freeform intent --dry-run
```

## Notes for implementors

- Replace placeholder tokens (`<command-name>`, `<agent-name>`, `<<work-item-id>>`, `<slug>`) before publishing.
- Keep the command's process narrowly scoped: prefer multiple focused commands over one large, monolithic agent.
- Add domain-specific hard requirements where needed (privacy, compliance, license constraints).

- Frontmatter keys supported by OpenCode command markdown: `description` (shown in TUI), `agent`, `model`, `subtask`. The file content is used as the `template`.
- Use `--dry-run` behavior to preview changes; implementors should honor that flag in the command's logic.

(End of template)
