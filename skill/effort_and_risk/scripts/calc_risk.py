#!/usr/bin/env python3
"""
calc_risk.py

Compute risk aggregation given probability/impact for parent and children and a certainty score.

Inputs (stdin JSON):
  parent: { probability, impact }
  children: list of { id, probability, impact }
  certainty: 0-100

Output: risk object with aggregated probability, impact, score, level, top_drivers, mitigations
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


def main():
    data = json.load(sys.stdin)
    parent = data.get("parent", {})
    children = data.get("children", [])
    certainty = float(data.get("certainty", 100))

    # Simple aggregation: take max probability and max impact among parent+children weighted by certainty
    probs = [parent.get("probability", 0)] + [c.get("probability", 0) for c in children]
    imps = [parent.get("impact", 0)] + [c.get("impact", 0) for c in children]

    # Adjust influence by certainty: lower certainty increases effective probability by up to 10%
    certainty_factor = 1.0 + max(0, (100 - certainty) / 100.0) * 0.1

    agg_prob = min(5, max(probs) * certainty_factor)
    agg_imp = min(5, max(imps) * certainty_factor)

    score = int(round(agg_prob * agg_imp))
    level = level_from_score(score)

    # Top drivers: pick top 3 child issues by probability*impact
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

    out = {
        "probability": round(agg_prob, 2),
        "impact": round(agg_imp, 2),
        "score": score,
        "level": level,
        "top_drivers": top,
        "mitigations": mitigations,
    }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
