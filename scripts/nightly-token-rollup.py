#!/usr/bin/env python3
"""
Nightly token usage rollup script.

Runs after nightly-consolidation (cron: 5 3 * * *).

Steps:
1. Read all session JSONLs modified in the past 24h to get dispatcher token usage.
2. Read ~/lobster-workspace/logs/token-usage.jsonl for per-subagent breakdown.
3. Compute per-day totals: dispatcher vs subagent, input vs output.
4. Flag sessions approaching 80% context window fill (>= 160k tokens).
5. Write daily summary to ~/lobster-workspace/logs/token-daily.jsonl.
6. Compute weekly rollup (last 7 daily entries) and write to token-weekly.jsonl.

## Output: token-daily.jsonl (one line per day, append-only)

  {
    "date": "2026-06-21",
    "dispatcher_input": 50000000,
    "dispatcher_output": 200000,
    "subagent_input": 19000000,
    "subagent_output": 100000,
    "total": 69300000,
    "subagent_count": 18,
    "context_fills": [{"session_id": "...", "pct": 0.87}],
    "top_agents_by_tokens": [{"task_id": "...", "tokens": 2000000, "agent_id": "..."}],
    "hourly": {"00": 1000000, "01": 500000, ...}
  }

## Output: token-weekly.jsonl (one line per week, rolling 7-day window)

  {
    "week_ending": "2026-06-21",
    "total": 485000000,
    "dispatcher_input": 350000000,
    "dispatcher_output": 1400000,
    "subagent_input": 133000000,
    "subagent_output": 700000,
    "subagent_count": 126,
    "days": 7
  }
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME = Path.home()
_WORKSPACE = _HOME / "lobster-workspace"
_LOG_DIR = _WORKSPACE / "logs"
_TOKEN_USAGE_LOG = _LOG_DIR / "token-usage.jsonl"
_TOKEN_DAILY_LOG = _LOG_DIR / "token-daily.jsonl"
_TOKEN_WEEKLY_LOG = _LOG_DIR / "token-weekly.jsonl"
_CLAUDE_PROJECTS = _HOME / ".claude" / "projects" / "-home-lobster-lobster-workspace"

# Context window config
_CONTEXT_WINDOW = 200_000          # tokens per session
_HIGH_CONTEXT_THRESHOLD = 0.80     # 80% = flag as "high context"
_HIGH_CONTEXT_TOKENS = int(_CONTEXT_WINDOW * _HIGH_CONTEXT_THRESHOLD)  # 160_000

# Rolling window for weekly rollup
_WEEKLY_ROLLUP_DAYS = 7


# ---------------------------------------------------------------------------
# Dispatcher JSONL parsing (session-level files)
# ---------------------------------------------------------------------------

def _is_recent(path: Path, cutoff: datetime) -> bool:
    """Return True if the file was modified at or after cutoff."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime >= cutoff
    except OSError:
        return False


def _parse_dispatcher_jsonl(path: Path) -> dict:
    """Parse a dispatcher session JSONL and return token usage across all turns.

    Returns: {input_tokens, output_tokens, session_id, max_context_tokens, model}
    max_context_tokens is the peak total (input+cache) across any single assistant
    turn -- used to detect high context sessions.
    """
    input_tokens = 0
    output_tokens = 0
    max_context_tokens = 0
    model = "unknown"
    session_id = path.stem  # filename without .jsonl is the session UUID

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # Dispatcher sessions use: {type: "assistant", message: {role, usage, model}}
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant" and "usage" in msg:
                        usage = msg["usage"] or {}
                        inp = usage.get("input_tokens", 0) or 0
                        cache_create = usage.get("cache_creation_input_tokens", 0) or 0
                        cache_read = usage.get("cache_read_input_tokens", 0) or 0
                        out = usage.get("output_tokens", 0) or 0
                        total_inp = inp + cache_create + cache_read
                        input_tokens += total_inp
                        output_tokens += out
                        if total_inp > max_context_tokens:
                            max_context_tokens = total_inp
                        m = msg.get("model")
                        if m:
                            model = m
    except OSError:
        pass

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "max_context_tokens": max_context_tokens,
        "session_id": session_id,
        "model": model,
    }


def collect_dispatcher_stats(cutoff: datetime) -> dict:
    """Collect dispatcher token usage from all session JSONLs modified since cutoff.

    Returns:
      {
        "input": int,
        "output": int,
        "context_fills": [{"session_id": str, "pct": float}]
      }
    """
    if not _CLAUDE_PROJECTS.is_dir():
        return {"input": 0, "output": 0, "context_fills": []}

    total_input = 0
    total_output = 0
    context_fills = []

    for path in _CLAUDE_PROJECTS.iterdir():
        # Session JSONLs are directly under -home-lobster-lobster-workspace/
        # named <uuid>.jsonl (not in subdirectories)
        if not path.is_file() or path.suffix != ".jsonl":
            continue
        if not _is_recent(path, cutoff):
            continue

        stats = _parse_dispatcher_jsonl(path)
        total_input += stats["input_tokens"]
        total_output += stats["output_tokens"]

        if stats["max_context_tokens"] >= _HIGH_CONTEXT_TOKENS:
            pct = stats["max_context_tokens"] / _CONTEXT_WINDOW
            context_fills.append({"session_id": stats["session_id"], "pct": round(pct, 3)})

    context_fills.sort(key=lambda x: x["pct"], reverse=True)

    return {
        "input": total_input,
        "output": total_output,
        "context_fills": context_fills,
    }


# ---------------------------------------------------------------------------
# Subagent token-usage.jsonl parsing
# ---------------------------------------------------------------------------

def _parse_token_usage_jsonl(date_str: str) -> dict:
    """Read token-usage.jsonl and return stats for entries matching date_str.

    date_str: "YYYY-MM-DD"

    Returns:
      {
        "input": int,
        "cache_creation": int,
        "cache_read": int,
        "output": int,
        "subagent_count": int,
        "top_agents": [{"task_id": str, "tokens": int, "agent_id": str}],
        "hourly": {"00": int, "01": int, ...}
      }
    """
    if not _TOKEN_USAGE_LOG.exists():
        return {
            "input": 0, "cache_creation": 0, "cache_read": 0, "output": 0,
            "subagent_count": 0, "top_agents": [], "hourly": {}
        }

    total_input = 0
    total_cache_creation = 0
    total_cache_read = 0
    total_output = 0
    subagent_count = 0
    agent_totals: list[dict] = []
    hourly: dict[str, int] = defaultdict(int)

    try:
        with _TOKEN_USAGE_LOG.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                ts = record.get("ts", "")
                if not ts.startswith(date_str):
                    continue

                inp = record.get("input_tokens", 0) or 0
                cc = record.get("cache_creation_tokens", 0) or 0
                cr = record.get("cache_read_tokens", 0) or 0
                out = record.get("output_tokens", 0) or 0
                agent_total = inp + cc + cr + out

                total_input += inp
                total_cache_creation += cc
                total_cache_read += cr
                total_output += out
                subagent_count += 1

                agent_totals.append({
                    "task_id": record.get("task_id"),
                    "agent_id": record.get("agent_id"),
                    "tokens": agent_total,
                })

                # Hourly breakdown: extract hour from ISO timestamp
                try:
                    hour = ts[11:13]  # "HH" from "YYYY-MM-DDTHH:MM:SS..."
                    hourly[hour] += agent_total
                except (IndexError, ValueError):
                    pass

    except OSError:
        pass

    top_agents = sorted(agent_totals, key=lambda x: x["tokens"], reverse=True)[:10]

    return {
        "input": total_input,
        "cache_creation": total_cache_creation,
        "cache_read": total_cache_read,
        "output": total_output,
        "subagent_count": subagent_count,
        "top_agents": top_agents,
        "hourly": dict(hourly),
    }


# ---------------------------------------------------------------------------
# Daily summary construction
# ---------------------------------------------------------------------------

def build_daily_summary(date_str: str) -> dict:
    """Build the daily token summary for the given date.

    Combines dispatcher stats (from session JSONLs modified in the past 24h)
    and subagent stats (from token-usage.jsonl entries for the date).
    """
    # Cutoff: start of the given date in UTC
    date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    cutoff = date  # include entire day

    dispatcher = collect_dispatcher_stats(cutoff)
    subagent = _parse_token_usage_jsonl(date_str)

    dispatcher_input = dispatcher["input"]
    dispatcher_output = dispatcher["output"]
    subagent_input = subagent["input"] + subagent["cache_creation"] + subagent["cache_read"]
    subagent_output = subagent["output"]
    total = dispatcher_input + dispatcher_output + subagent_input + subagent_output

    return {
        "date": date_str,
        "dispatcher_input": dispatcher_input,
        "dispatcher_output": dispatcher_output,
        "subagent_input": subagent_input,
        "subagent_output": subagent_output,
        "total": total,
        "subagent_count": subagent["subagent_count"],
        "context_fills": dispatcher["context_fills"],
        "top_agents_by_tokens": subagent["top_agents"],
        "hourly": subagent["hourly"],
    }


# ---------------------------------------------------------------------------
# Daily JSONL I/O
# ---------------------------------------------------------------------------

def _read_daily_log() -> list[dict]:
    """Read all entries from token-daily.jsonl, newest first."""
    if not _TOKEN_DAILY_LOG.exists():
        return []
    entries = []
    try:
        with _TOKEN_DAILY_LOG.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entries.append(json.loads(raw_line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def _upsert_daily_entry(entry: dict) -> None:
    """Append or replace the daily entry for entry['date'] in token-daily.jsonl.

    Reads the existing file, replaces any existing entry for the same date,
    then rewrites. This keeps the file append-friendly but idempotent on re-runs.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read_daily_log()
    date = entry["date"]
    updated = [e for e in existing if e.get("date") != date]
    updated.append(entry)
    # Sort chronologically
    updated.sort(key=lambda e: e.get("date", ""))

    with _TOKEN_DAILY_LOG.open("w", encoding="utf-8") as fh:
        for e in updated:
            fh.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Weekly rollup
# ---------------------------------------------------------------------------

def build_weekly_rollup(week_ending: str) -> dict:
    """Compute the rolling 7-day total ending on week_ending (YYYY-MM-DD).

    Reads the last _WEEKLY_ROLLUP_DAYS entries from token-daily.jsonl
    that fall within the window.
    """
    end_date = datetime.strptime(week_ending, "%Y-%m-%d")
    start_date = end_date - timedelta(days=_WEEKLY_ROLLUP_DAYS - 1)
    start_str = start_date.strftime("%Y-%m-%d")

    entries = _read_daily_log()
    window = [
        e for e in entries
        if start_str <= e.get("date", "") <= week_ending
    ]

    dispatcher_input = sum(e.get("dispatcher_input", 0) for e in window)
    dispatcher_output = sum(e.get("dispatcher_output", 0) for e in window)
    subagent_input = sum(e.get("subagent_input", 0) for e in window)
    subagent_output = sum(e.get("subagent_output", 0) for e in window)
    total = sum(e.get("total", 0) for e in window)
    subagent_count = sum(e.get("subagent_count", 0) for e in window)

    return {
        "week_ending": week_ending,
        "total": total,
        "dispatcher_input": dispatcher_input,
        "dispatcher_output": dispatcher_output,
        "subagent_input": subagent_input,
        "subagent_output": subagent_output,
        "subagent_count": subagent_count,
        "days": len(window),
    }


def _upsert_weekly_entry(entry: dict) -> None:
    """Append or replace the weekly entry for entry['week_ending'] in token-weekly.jsonl."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if _TOKEN_WEEKLY_LOG.exists():
        try:
            with _TOKEN_WEEKLY_LOG.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        existing.append(json.loads(raw_line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    week_ending = entry["week_ending"]
    updated = [e for e in existing if e.get("week_ending") != week_ending]
    updated.append(entry)
    updated.sort(key=lambda e: e.get("week_ending", ""))

    with _TOKEN_WEEKLY_LOG.open("w", encoding="utf-8") as fh:
        for e in updated:
            fh.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"[nightly-token-rollup] Running for date: {today}")

    daily = build_daily_summary(today)
    _upsert_daily_entry(daily)
    print(
        f"[nightly-token-rollup] Daily: total={daily['total']:,} "
        f"dispatcher_in={daily['dispatcher_input']:,} "
        f"subagent_in={daily['subagent_input']:,} "
        f"subagents={daily['subagent_count']} "
        f"high_context_sessions={len(daily['context_fills'])}"
    )

    weekly = build_weekly_rollup(today)
    _upsert_weekly_entry(weekly)
    print(
        f"[nightly-token-rollup] Weekly ({weekly['days']}d): "
        f"total={weekly['total']:,} subagents={weekly['subagent_count']}"
    )

    print("[nightly-token-rollup] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[nightly-token-rollup] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
