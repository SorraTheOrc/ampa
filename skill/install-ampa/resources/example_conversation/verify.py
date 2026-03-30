"""Verification script: ensures transcript has expected messages and alternation."""

import json
import sys


def verify(path: str, rounds: int) -> int:
    expected = rounds * 2
    with open(path, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]

    if len(lines) != expected:
        print(f"FAIL: expected {expected} messages, found {len(lines)}")
        return 2

    # check alternation
    for i, e in enumerate(lines):
        sender = e.get("sender")
        expected_sender = "AMPA" if i % 2 == 0 else "BUILD"
        if sender != expected_sender:
            print(
                f"FAIL: message {i + 1} sender {sender} != expected {expected_sender}"
            )
            return 3

    print("OK: transcript verification passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: verify.py <transcript.jsonl> [rounds]")
        sys.exit(1)
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    sys.exit(verify(sys.argv[1], rounds))
