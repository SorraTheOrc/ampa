#!/usr/bin/env python3
"""
orchestrate_estimate.py

Take inputs (o, m, p, overheads, parent risk, children risks, certainty, assumptions, unknowns)
and produce the final JSON output used by the skill. Also prints the JSON to stdout.

Input: JSON via stdin with keys:
  o, m, p (numbers, hours)
  items: optional list of per-work-item estimates (each item: {id,title,o,m,p})
  overheads: { coordination: n, review: n, testing: n, risk_buffer: n }
  parent: { probability, impact }
  children: [ { id, title, probability, impact } ]
  certainty: 0-100
  confidence_percent (optional override)
  assumptions (list)
  unknowns (list)

Output: final JSON block written to stdout
"""

import sys
import json


def level_from_score(score):
    if score <= 5:
        return "Low"
    if score <= 12:
        return "Medium"
    if score <= 19:
        return "High"
    return "Critical"


def pick_tshirt(hours, thresholds):
    for size, bounds in thresholds.items():
        mn = bounds.get("min", 0)
        mx = bounds.get("max")
        if mx is None:
            if hours >= mn:
                return size
        else:
            if hours >= mn and hours < mx:
                return size
    return "XS"


def compute_omp(data):
    items = data.get("items")
    if isinstance(items, list) and items:
        o_sum = sum(float(i.get("o", 0)) for i in items)
        m_sum = sum(float(i.get("m", 0)) for i in items)
        p_sum = sum(float(i.get("p", 0)) for i in items)
        return o_sum, m_sum, p_sum
    return (
        float(data.get("o", 0)),
        float(data.get("m", 0)),
        float(data.get("p", 0)),
    )


def main():
    data = json.load(sys.stdin)

    o, m, p = compute_omp(data)
    overheads = data.get("overheads", {})

    expected = (o + 4 * m + p) / 6.0
    overheads_total = sum(float(v) for v in overheads.values()) if overheads else 0.0
    recommended = expected + overheads_total
    range_min = o + overheads_total
    range_max = p + overheads_total

    # Ensure an issue_id is provided so we can inspect its stage before computing risk
    issue_id = data.get("issue_id")
    if not issue_id:
        print(json.dumps({"error": "missing required field: issue_id"}))
        sys.exit(2)

    # Verify the issue stage (accept plan_complete or intake_complete). If intake, we'll
    # scale down the provided certainty so downstream calculations reflect less-detailed planning.
    try:
        import subprocess

        show_proc = subprocess.run(
            ["wl", "show", issue_id, "--json"], capture_output=True, text=True
        )
        if show_proc.returncode != 0:
            print(json.dumps({"error": "wl show failed", "stdout": show_proc.stdout, "stderr": show_proc.stderr}))
            sys.exit(3)
        show_json = json.loads(show_proc.stdout)
        stage = show_json.get("workItem", {}).get("stage", "")
        if stage not in ("plan_complete", "intake_complete"):
            # Per SKILL gating, output single sentence refusal
            print(
                f"The issue does not have a sufficiently detailed plan, to proceed it must be in the stage of `intake_complete` or `plan_complete`. Run the intake command with `/intake {issue_id}` or the plan command with `/plan {issue_id}`."
            )
            sys.exit(4)
        input_stage = stage
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(5)

    # load tshirt thresholds
    try:
        with open("references/t-shirt_sizes.json", "r") as f:
            tshirt_cfg = json.load(f)
            thresholds = tshirt_cfg.get("thresholds", {})
    except Exception:
        thresholds = {
            "XS": {"min": 0, "max": 4},
            "S": {"min": 4, "max": 24},
            "M": {"min": 24, "max": 80},
            "L": {"min": 80, "max": 240},
            "XL": {"min": 240, "max": None},
        }

    tshirt = pick_tshirt(recommended, thresholds)
    # Expand shorthand codes to full-text labels for clarity/auditability
    tshirt_map = {
        "XS": "Extra Small",
        "S": "Small",
        "M": "Medium",
        "L": "Large",
        "XL": "Extra Large",
    }
    tshirt = tshirt_map.get(tshirt, tshirt)

    # risk aggregation
    parent = data.get("parent", {})
    children = data.get("children", [])
    certainty = float(data.get("certainty", 100))

    # If the issue is only at intake, scale certainty down to reflect lower confidence
    original_certainty = certainty
    if input_stage == "intake_complete":
        certainty = certainty * 0.6

    probs = [parent.get("probability", 0)] + [c.get("probability", 0) for c in children]
    imps = [parent.get("impact", 0)] + [c.get("impact", 0) for c in children]
    certainty_factor = 1.0 + max(0, (100 - certainty) / 100.0) * 0.1
    agg_prob = min(5, max(probs) * certainty_factor)
    agg_imp = min(5, max(imps) * certainty_factor)
    score = int(round(agg_prob * agg_imp))
    level = level_from_score(score)

    drivers = []
    for c in children:
        drivers.append(
            (
                c.get("id", ""),
                c.get("probability", 0) * c.get("impact", 0),
                c.get("title", ""),
            )
        )
    drivers.sort(key=lambda x: x[1], reverse=True)
    top = [d[2] or d[0] for d in drivers[:3]]

    mitigations = [
        "Add targeted tests and integration checks",
        "Lock dependencies and add compatibility tests",
        "Schedule extra review for risky components",
    ]

    risk = {
        "probability": round(agg_prob, 2),
        "impact": round(agg_imp, 2),
        "score": score,
        "level": level,
        "top_drivers": top,
        "mitigations": mitigations,
    }

    final = {
        "effort": {
            "unit": "hours",
            "tshirt": tshirt,
            "o": o,
            "m": m,
            "p": p,
            "expected": round(expected, 2),
            "recommended": round(recommended, 2),
            "range": [round(range_min, 2), round(range_max, 2)],
        },
        "risk": risk,
        "confidence_percent": int(
            data.get("confidence_percent", round(100 - (100 - certainty) / 2))
        ),
        "assumptions": data.get("assumptions", []),
        "unknowns": data.get("unknowns", []),
    }

    # Attach audit fields describing the input stage and any certainty adjustment
    final["input_stage"] = input_stage
    final["original_certainty"] = original_certainty
    final["adjusted_certainty"] = certainty

    # The orchestration script MUST update the issue's effort and risk fields.
    issue_id = data.get("issue_id")
    if not issue_id:
        print(json.dumps({"error": "missing required field: issue_id"}))
        sys.exit(2)

    # Verify the issue is in intake_complete or plan_complete stage before applying updates
    try:
        import subprocess

        show_proc = subprocess.run(
            ["wl", "show", issue_id, "--json"], capture_output=True, text=True
        )
        if show_proc.returncode != 0:
            final["update_result"] = {
                "success": False,
                "error": "wl show failed",
                "stdout": show_proc.stdout,
                "stderr": show_proc.stderr,
            }
            print(json.dumps(final))
            sys.exit(3)
        show_json = json.loads(show_proc.stdout)
        stage = show_json.get("workItem", {}).get("stage", "")
        if stage not in ("plan_complete", "intake_complete"):
            # Per SKILL gating, output single sentence refusal
            print(
                f"The issue does not have a sufficiently detailed plan, to proceed it must be in the stage of `intake_complete` or `plan_complete`. Run the intake command with `/intake {issue_id}` or the plan command with `/plan {issue_id}`."
            )
            sys.exit(4)
    except Exception as e:
        final["update_result"] = {"success": False, "error": str(e)}
        print(json.dumps(final))
        sys.exit(5)

    # Map risk.level to wl's risk label (Critical -> Severe)
    risk_level = risk.get("level", "")
    wl_risk_map = {
        "Low": "Low",
        "Medium": "Medium",
        "High": "High",
        "Critical": "Severe",
        "Severe": "Severe",
    }
    wl_risk = wl_risk_map.get(risk_level, "Medium")

    # Use tshirt as effort value (full-text, e.g. "Small", "Extra Large")
    wl_effort = tshirt

    # Run wl update to set effort and risk; this is mandatory
    try:
        import subprocess

        update_cmd = [
            "wl",
            "update",
            issue_id,
            "--effort",
            str(wl_effort),
            "--risk",
            str(wl_risk),
            "--json",
        ]
        update_proc = subprocess.run(update_cmd, capture_output=True, text=True)
        update_out = update_proc.stdout
        update_err = update_proc.stderr
        update_success = update_proc.returncode == 0
        # attach the raw results
        final["update_result"] = {
            "success": update_success,
            "returncode": update_proc.returncode,
            "stdout": update_out,
            "stderr": update_err,
        }
    except Exception as e:
        final["update_result"] = {"success": False, "error": str(e)}

    # Now render the human-readable table and post it as a comment (mandatory)
    try:
        import subprocess
        import tempfile

        # Build a sanitized object for rendering to avoid contaminating the human text
        sanitized = {
            "effort": final.get("effort"),
            "risk": final.get("risk"),
            "confidence_percent": final.get("confidence_percent"),
            "assumptions": final.get("assumptions"),
            "unknowns": final.get("unknowns"),
        }

        import os

        script_dir = os.path.dirname(__file__)
        json_to_human_path = os.path.join(script_dir, "json_to_human.py")
        sj = json.dumps(sanitized)
        p = subprocess.run(
            ["python3", json_to_human_path], input=sj, text=True, capture_output=True
        )
        human_text = p.stdout or ""
        final["human_text"] = human_text
        final["human_render_rc"] = p.returncode
        final["human_render_stderr"] = p.stderr

        if not human_text.strip():
            final["comment_result"] = {
                "success": False,
                "error": "empty rendered human text",
                "human_render_stderr": p.stderr,
            }
        else:
            # Build the JSON block to include after the human text
            skill_json = {
                "effort": final.get("effort"),
                "risk": final.get("risk"),
                "confidence_percent": final.get("confidence_percent"),
                "assumptions": final.get("assumptions"),
                "unknowns": final.get("unknowns"),
            }
            skill_json_str = json.dumps(skill_json, indent=2)

            combined_text = human_text + "\n\n```json\n" + skill_json_str + "\n```"

            # Pass the combined text directly to `wl comment add` as an argument
            # Avoid using shell or temporary files with fixed names.
            try:
                comment_cmd = [
                    "wl",
                    "comment",
                    "add",
                    issue_id,
                    "--author",
                    "effort_and_risk_skill",
                    "--comment",
                    combined_text,
                    "--json",
                ]
                comment_proc = subprocess.run(
                    comment_cmd, capture_output=True, text=True
                )
                final["comment_result"] = {
                    "returncode": comment_proc.returncode,
                    "stdout": comment_proc.stdout,
                    "stderr": comment_proc.stderr,
                    "success": comment_proc.returncode == 0,
                }
            except Exception as e:
                final["comment_result"] = {"success": False, "error": str(e)}
    except Exception as e:
        final["comment_result"] = {"success": False, "error": str(e)}

    print(json.dumps(final))


if __name__ == "__main__":
    main()
