---
name: effort_and_risk
description: "Produce engineering effort and risk estimates using WBS, three-point (PERT) estimating, risk matrix, uncertainty, and assumptions. Operates on a provided issue id and its prepared plan."
---

Purpose
-------
Produce a concise, auditable engineering estimate (effort + risk) for a prepared work item. The skill's canonical outputs are:

- A machine-readable JSON object containing effort (effort_units, tshirt size, O/M/P, expected, recommended, range), risk (probability, impact, score, level, top drivers, mitigations), confidence, assumptions, and unknowns.
 - A machine-readable JSON object containing effort (effort_units, tshirt size (full-text, e.g. "Small", "Extra Large"), O/M/P, expected, recommended, range), risk (probability, impact, score, level, top drivers, mitigations), confidence, assumptions, and unknowns.
- A human-readable summary generated and posted by the orchestrator; the posted content is included in the orchestrator output.

Gating (mandatory)
-------------------

Before doing any work the issue MUST be in the `intake_complete` or `plan_complete` stage. If it is not, refuse and output ONLY this single sentence (replace <issue-id> with the actual id):

The issue does not have a sufficiently detailed plan, to proceed it must be in the stage of `intake_complete` or `plan_complete`. Run the intake command with `/intake <issue-id>` or the plan command with `/plan <issue-id>`.

Do not output any other text when refusing.

Orchestrator behavior
---------------------

The `orchestrate_estimate.py` script accepts work items in either `intake_complete` or `plan_complete` stages when applying effort and risk updates. This allows estimates to be applied early in the intake phase and refined later if needed. The script uses the same stage validation for both calculation and update phases.

When to use
-----------
Use this skill only after the Producer has prepared a plan and set the work item's stage to `intake_complete` or `plan_complete`.

Required inputs (what you must prepare before running the scripts)
----------------------------------------------------------------
- issue id (string). Fetch the full issue and its children for auditability: wl show <issue-id> --json
- A lightweight WBS. Use the issue's child work items as the WBS source (children are returned recursively by wl show --json). If the issue has no children and the scope is small, the parent issue itself can be treated as the WBS.
- Provide Optimistic (O), Most Likely (M), and Pessimistic (P) estimates in effort_units for the overall work scope. Optionally (for traceability), provide O/M/P per WBS item or per child issue; the scripts will aggregate per-item inputs into the overall estimate when present.
- Explicit additive overheads (effort_units): coordination, review, testing/integration, risk buffer. These MUST be listed separately (do not hide them inside O/M/P).
- Parent and child risk inputs: for the parent issue and for each child, a Probability (1–5) and Impact (1–5). Include short titles for children to aid triage.
- Certainty % (0–100) representing the assessor's confidence in the provided inputs.
- Clear lists of assumptions and unknowns (each as short strings).

Principles (kept brief)
-----------------------
- Use effort_units as the canonical unit.
- Use three-point (PERT) estimating for expected value: E = (O + 4*M + P) / 6.
- Surface assumptions and unknowns explicitly so reviewers can decide if further planning (spikes) is needed.
- T-shirt sizing boundaries are defined in references/t-shirt_sizes.json; scripts use that file to pick sizes.

Canonical workflow (minimal, authoritative)
-----------------------------------------
Follow these steps from the project root (run commands from the repository root, not from the `skill/effort_and_risk` directory):

1) Fetch the issue and its children (audit file):

   wl show <issue-id> --json

2) Prepare the inputs (JSON) using the plan and WBS. The input should include keys such as:

   {
     "items": [{"id":"CHILD-1","title":"Design","o":2,"m":4,"p":6}, ...],
     "o": <effort_units>, "m": <effort_units>, "p": <effort_units>,
     "overheads": {"coordination": <h>, "review": <h>, "testing": <h>, "risk_buffer": <h>},
     "parent": {"probability": <1-5>, "impact": <1-5>},
     "children": [{"id":"ISSUE-1","probability":2,"impact":1,"title":"child A"}, ...],
     "certainty": 85,
     "assumptions": ["..."],
     "unknowns": ["..."]
   }

3) Run the orchestrator. Prefer capturing output to a filename derived from the work-item id (avoid fixed names):

       ```sh
       python3 scripts/run_skill.py --issue <issue-id> <<'JSON' > final-<issue-id>.json
       { ... }
       JSON
       ```

    The orchestrator enforces gating, computes effort and risk, updates issue metadata, and posts the comment. The script returns a single JSON object that includes:
   - human_text (the content of the posted comment)
   - comment_result (CLI response details)

4) Verify what was posted by inspecting the generated output file (for example `final-<issue-id>.json`) or, if needed, the issue itself:

   wl show <issue-id> --format full

Outputs
-------
- `final-<issue-id>.json` (or captured stdout): canonical machine-readable estimate (as described above), plus orchestration metadata including human_text and comment_result. Prefer filenames generated from the work-item id to avoid fixed temporary filenames.

References (bundled)
--------------------
- references/t-shirt_sizes.json — T-shirt thresholds used by scripts
- scripts/calc_effort.py
- scripts/calc_risk.py
- scripts/calc_effort_with_risk.py
- scripts/assemble_json.py
- scripts/json_to_human.py
- scripts/orchestrate_estimate.py

