#!/usr/bin/env python3
"""
run_skill.py

Convenience wrapper to run the canonical skill flow for an issue.

Usage (example):
  python3 run_skill.py --issue SA-0MKT9O9AY002COU8 --o 2 --m 4 --p 8 --coord 1 --review 1 --testing 1 --risk_buffer 1 --certainty 85

The script will:
 - fetch the issue and its children with `wl show <issue> --children --json`
  - assemble a JSON payload using provided O/M/P (and optional per-item estimates) and overheads (defaults applied if omitted)
 - call orchestrate_estimate.py with the assembled payload and print the final JSON output

This enforces the canonical scripts are used (orchestrate_estimate.py + json_to_human.py) so comments and updates are consistent.
"""

import argparse
import json
import subprocess
import sys
import os


def wl_show(issue_id):
    p = subprocess.run(
        ["wl", "show", issue_id, "--children", "--json"], capture_output=True, text=True
    )
    if p.returncode != 0:
        print(json.dumps({"error": "wl show failed", "stderr": p.stderr}))
        sys.exit(2)
    return json.loads(p.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", required=True)
    parser.add_argument("--o", type=float, default=2.0)
    parser.add_argument("--m", type=float, default=4.0)
    parser.add_argument("--p", type=float, default=8.0)
    parser.add_argument("--coord", type=float, default=1.0)
    parser.add_argument("--review", type=float, default=1.0)
    parser.add_argument("--testing", type=float, default=1.0)
    parser.add_argument("--risk_buffer", type=float, default=1.0)
    parser.add_argument("--certainty", type=float, default=85.0)
    parser.add_argument("--parent_prob", type=float, default=3.0)
    parser.add_argument("--parent_imp", type=float, default=3.0)
    parser.add_argument("--assumptions", type=str, default="[]")
    parser.add_argument("--unknowns", type=str, default="[]")
    args = parser.parse_args()

    issue_id = args.issue
    show = wl_show(issue_id)

    def flatten_children(children):
        out = []
        for c in children or []:
            out.append(c)
            out.extend(flatten_children(c.get("children", [])))
        return out

    # Collect children (recursive) if present
    children_nodes = flatten_children(show.get("children", []))
    children_info = []
    for c in children_nodes:
        cid = c.get("id")
        title = c.get("title", "")
        children_info.append({"id": cid, "title": title, "probability": 2, "impact": 1})

    payload = {
        "o": args.o,
        "m": args.m,
        "p": args.p,
        "overheads": {
            "coordination": args.coord,
            "review": args.review,
            "testing": args.testing,
            "risk_buffer": args.risk_buffer,
        },
        "parent": {"probability": args.parent_prob, "impact": args.parent_imp},
        "children": children_info,
        "certainty": args.certainty,
        "assumptions": json.loads(args.assumptions),
        "unknowns": json.loads(args.unknowns),
        "issue_id": issue_id,
    }

    if not sys.stdin.isatty():
        try:
            stdin_payload = json.load(sys.stdin)
            if isinstance(stdin_payload, dict):
                payload.update(stdin_payload)
        except Exception:
            pass

    # Call orchestrate_estimate.py located in the same scripts directory
    script_dir = os.path.dirname(__file__)
    orchestrator = os.path.join(script_dir, "orchestrate_estimate.py")
    proc = subprocess.run(
        ["python3", orchestrator],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print(
            json.dumps(
                {
                    "error": "orchestrator failed",
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                }
            )
        )
        sys.exit(3)

    # Print orchestrator output
    print(proc.stdout)


if __name__ == "__main__":
    main()
