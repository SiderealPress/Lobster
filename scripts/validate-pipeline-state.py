#!/usr/bin/env python3
"""
Validates a pipeline-state.json file against the required schema.
Usage: uv run validate-pipeline-state.py <path-to-pipeline-state.json>
Exits 0 if valid, 1 if invalid.
"""

import json
import sys
from pathlib import Path

REQUIRED_FIELDS = {
    'author': str,
    'project': str,
    'skill_valid': bool,
    'current_round': int,
    'writing_modes_used': list,
}

LOOP_GATE_FIELDS = {
    'skill_valid': True,  # Must be True to enter The Loop
}

def validate(path: str) -> tuple[bool, list[str]]:
    errors = []
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception as e:
        return False, [f"Cannot read file: {e}"]

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in state:
            errors.append(f"Missing required field: {field}")
        elif not isinstance(state[field], expected_type):
            errors.append(f"Field '{field}' must be {expected_type.__name__}, got {type(state[field]).__name__}")

    for field, required_value in LOOP_GATE_FIELDS.items():
        if state.get(field) != required_value:
            errors.append(f"LOOP GATE: '{field}' must be {required_value} before entering The Loop (got {state.get(field)})")

    return len(errors) == 0, errors

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: validate-pipeline-state.py <path>")
        sys.exit(1)

    valid, errors = validate(sys.argv[1])
    if valid:
        print("✅ pipeline-state.json is valid")
        sys.exit(0)
    else:
        print("❌ pipeline-state.json validation failed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
