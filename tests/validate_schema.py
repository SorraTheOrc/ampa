#!/usr/bin/env python3
"""
Validate workflow.json against workflow-schema.json using JSON Schema.

CI-ready script: exit 0 on success, exit 1 on validation errors, exit 2 on file errors.

Usage:
    python tests/validate_schema.py
    python tests/validate_schema.py --descriptor path/to/workflow.json --schema path/to/schema.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:
    print(
        "ERROR: jsonschema package required. Install with: pip install jsonschema",
        file=sys.stderr,
    )
    sys.exit(2)


def load_json(path: Path) -> dict:
    """Load and parse a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(2)


def validate(descriptor: dict, schema: dict) -> list[str]:
    """Validate descriptor against schema, returning list of error messages."""
    validator = Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(descriptor), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"  [{path}] {error.message}")
    return errors


def main():
    parser = argparse.ArgumentParser(
        description="Validate workflow descriptor against JSON Schema"
    )
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=Path(__file__).parent.parent / "docs" / "workflow" / "workflow.json",
        help="Path to workflow descriptor JSON file (default: docs/workflow/workflow.json)",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).parent.parent
        / "docs"
        / "workflow"
        / "workflow-schema.json",
        help="Path to JSON Schema file (default: docs/workflow/workflow-schema.json)",
    )
    args = parser.parse_args()

    print(f"Schema:     {args.schema}")
    print(f"Descriptor: {args.descriptor}")
    print()

    schema = load_json(args.schema)
    descriptor = load_json(args.descriptor)

    errors = validate(descriptor, schema)

    if errors:
        print(f"FAIL: {len(errors)} schema validation error(s):\n")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print("PASS: Workflow descriptor is valid against the schema.")
        sys.exit(0)


if __name__ == "__main__":
    main()
