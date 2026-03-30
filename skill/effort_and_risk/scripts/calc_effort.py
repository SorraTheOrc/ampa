#!/usr/bin/env python3
"""
calc_effort.py

Compute PERT expected values and assemble effort-related fields.

Inputs (CLI args or stdin JSON):
  o: optimistic hours (float)
  m: most likely hours (float)
  p: pessimistic hours (float)
  items: optional list of per-work-item estimates (each item: {id,title,o,m,p})
  overheads: dict of additive overheads in hours (coordination, review, testing, risk_buffer)

Output: JSON with keys: unit, o, m, p, expected, overheads_total, recommended, range, tshirt
"""

import sys
import json
import math


def pick_tshirt(hours, thresholds):
    for size, bounds in thresholds.items():
        mn = bounds.get("min")
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
    if not sys.stdin.isatty():
        data = json.load(sys.stdin)
    else:
        # minimal CLI arg support
        if len(sys.argv) < 4:
            print(json.dumps({"error": "provide o m p as args or JSON via stdin"}))
            sys.exit(1)
        data = {
            "o": float(sys.argv[1]),
            "m": float(sys.argv[2]),
            "p": float(sys.argv[3]),
            "overheads": {},
        }

    o, m, p = compute_omp(data)
    overheads = data.get("overheads", {})

    expected = (o + 4 * m + p) / 6.0
    overheads_total = sum(float(v) for v in overheads.values()) if overheads else 0.0

    # Recommended planning value: expected + overheads_total
    recommended = expected + overheads_total

    # Range: min = o + overheads_total, max = p + overheads_total
    range_min = o + overheads_total
    range_max = p + overheads_total

    # Load thresholds from references file
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

    out = {
        "unit": "hours",
        "o": o,
        "m": m,
        "p": p,
        "expected": round(expected, 2),
        "overheads_total": round(overheads_total, 2),
        "recommended": round(recommended, 2),
        "range": [round(range_min, 2), round(range_max, 2)],
        "tshirt": tshirt,
    }

    print(json.dumps(out))


if __name__ == "__main__":
    main()
