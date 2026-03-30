#!/usr/bin/env python3
"""
assemble_json.py

Assemble the final JSON output for the skill given inputs.

Accepts stdin JSON with keys:
- effort (output from calc_effort)
- risk (probability, impact, score, level, top_drivers, mitigations)
- confidence_percent
- assumptions (list)
- unknowns (list)

Prints the combined JSON object required by the skill output.
"""

import sys
import json


def main():
    data = json.load(sys.stdin)
    effort = data.get("effort", {})
    risk = data.get("risk", {})
    confidence = data.get("confidence_percent", 0)
    assumptions = data.get("assumptions", [])
    unknowns = data.get("unknowns", [])

    out = {
        "effort": {
            "unit": effort.get("unit", "hours"),
            "tshirt": effort.get("tshirt", ""),
            "o": effort.get("o", 0),
            "m": effort.get("m", 0),
            "p": effort.get("p", 0),
            "expected": effort.get("expected", 0),
            "recommended": effort.get("recommended", 0),
            "range": effort.get("range", [0, 0]),
        },
        "risk": {
            "probability": risk.get("probability", 0),
            "impact": risk.get("impact", 0),
            "score": risk.get("score", 0),
            "level": risk.get("level", ""),
            "top_drivers": risk.get("top_drivers", []),
            "mitigations": risk.get("mitigations", []),
        },
        "confidence_percent": confidence,
        "assumptions": assumptions,
        "unknowns": unknowns,
    }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
