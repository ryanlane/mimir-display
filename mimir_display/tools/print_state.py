"""Utility to print local device assignment state.

Run on device (inside venv) to inspect persisted scene assignment.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

def find_state_file() -> Path | None:
    # Common locations tried by client logic
    candidates = []
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / "device_state.json")
    candidates.append(Path("/var/lib/mimir-display") / "device_state.json")
    home = Path.home() / ".mimir" / "device_state.json"
    candidates.append(home)
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> int:
    state_file = find_state_file()
    if not state_file:
        print("No device_state.json found in expected locations.")
        return 1
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Failed to load {state_file}: {e}")
        return 2
    print(f"State file: {state_file}")
    print(json.dumps(data, indent=2))
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
