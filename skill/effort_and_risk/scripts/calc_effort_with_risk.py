#!/usr/bin/env python3
"""
calc_effort_with_risk.py

Take O, M, P estimates and a risk input (either numeric score or a risk object)
and return the final JSON results block (effort + risk + confidence/assumptions/unknowns).

Input via stdin JSON:
  o, m, p (numbers, hours)
  items: optional list of per-work-item estimates (each item: {id,title,o,m,p})
  overheads: { coordination, review, testing, risk_buffer }
  risk: either { probability, impact, score } or a numeric "score"
  confidence_percent (optional)
  assumptions (list), unknowns (list)

Output: final JSON block printed to stdout
"""

import sys
import json
import math


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


def approx_pi_from_score(score):
    # Approximate probability and impact from a 1-25 score by sqrt
    v = max(1, min(25, int(round(score))))
    p = int(math.ceil(math.sqrt(v)))
    if p > 5:
        p = 5
    return p, p


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
    risk_in = data.get("risk")

    expected = (o + 4 * m + p) / 6.0
    overheads_total = sum(float(v) for v in overheads.values()) if overheads else 0.0
    recommended = expected + overheads_total
    range_min = o + overheads_total
    range_max = p + overheads_total

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
    # Expand shorthand codes to full-text labels
    tshirt_map = {
        "XS": "Extra Small",
        "S": "Small",
        "M": "Medium",
        "L": "Large",
        "XL": "Extra Large",
    }
    tshirt = tshirt_map.get(tshirt, tshirt)

    # normalize risk
    if isinstance(risk_in, dict):
        probability = risk_in.get("probability", 0)
        impact = risk_in.get("impact", 0)
        score = risk_in.get("score", int(round(probability * impact)))
    elif isinstance(risk_in, (int, float)):
        score = int(round(risk_in))
        probability, impact = approx_pi_from_score(score)
    else:
        probability, impact = 0, 0
        score = 0

    level = "Low"
    if score <= 5:
        level = "Low"
    elif score <= 12:
        level = "Medium"
    elif score <= 19:
        level = "High"
    else:
        level = "Critical"

    out = {
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
        "risk": {
            "probability": probability,
            "impact": impact,
            "score": score,
            "level": level,
            "top_drivers": [],
            "mitigations": [],
        },
        "confidence_percent": int(data.get("confidence_percent", 0)),
        "assumptions": data.get("assumptions", []),
        "unknowns": data.get("unknowns", []),
    }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
