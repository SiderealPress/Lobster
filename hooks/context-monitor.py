#!/usr/bin/env python3
"""
PostToolUse hook: logs context window usage percentage.
Proof-of-concept to verify context_window data is available in hook stdin.
"""
import json
import sys
from pathlib import Path
from datetime import datetime


def main():
    try:
        data = json.load(sys.stdin)
        context = data.get("context_window", {})
        used_pct = context.get("used_percentage")
        remaining_pct = context.get("remaining_percentage")

        if used_pct is not None:
            log_dir = Path.home() / "lobster-workspace" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "context-monitor.log"

            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "tool": data.get("tool_name", "unknown"),
                "used_percentage": used_pct,
                "remaining_percentage": remaining_pct,
                "full_context_window": context,
            }

            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Never block tool use


if __name__ == "__main__":
    main()
