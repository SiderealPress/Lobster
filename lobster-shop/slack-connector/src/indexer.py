"""
Slack Indexer — async enrichment pipeline for JSONL message logs.

Three indexing layers, each at different cadences:
1. Keyword index (FTS5) — every 5 minutes, cursor-based incremental
2. Thread summaries (Haiku) — every 15 minutes, threads idle >2h
3. Topic clusters + action items (Haiku) — nightly at 2am UTC

Design principles:
- Pure data transformations separated from I/O boundaries
- Cost guardrails: env flag, batch limits, channel filtering
- Composable pipeline stages (read → filter → transform → write)
- Haiku calls isolated behind an invocation abstraction for testability
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.keyword_index import KeywordIndex, _filter_after_cursor, _max_ts
from src.log_store import SlackLogStore

log = logging.getLogger("slack-indexer")

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

_DEFAULT_WORKSPACE = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
)
_DEFAULT_CONNECTOR_ROOT = _DEFAULT_WORKSPACE / "slack-connector"

_MAX_MESSAGES_PER_HAIKU_BATCH = 50
_THREAD_IDLE_SECONDS = 2 * 60 * 60  # 2 hours

_INDEX_ENABLED_ENV = "LOBSTER_SLACK_INDEX_ENABLED"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_index_enabled() -> bool:
    """Check whether Haiku indexing is enabled via environment variable.

    Defaults to True if the env var is not set.
    """
    val = os.environ.get(_INDEX_ENABLED_ENV, "true").lower()
    return val in ("true", "1", "yes")


def _load_channel_config(config_path: Path) -> dict[str, dict[str, Any]]:
    """Load channels.yaml and return a mapping of channel_id -> config.

    Returns empty dict if file doesn't exist. Pure-ish (reads one file).
    """
    if not config_path.exists():
        return {}

    try:
        import yaml
    except ImportError:
        # If PyYAML not available, skip channel config filtering
        log.warning("PyYAML not installed; channel config filtering disabled")
        return {}

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    channels = data.get("channels", [])
    return {ch["id"]: ch for ch in channels if "id" in ch}


def _is_channel_ignored(
    channel_id: str, channel_config: dict[str, dict[str, Any]]
) -> bool:
    """Check if a channel is configured as mode: ignore.

    Pure function. Returns False if channel not in config (default: not ignored).
    """
    ch = channel_config.get(channel_id, {})
    return ch.get("mode", "monitor") == "ignore"


def _group_by_thread(
    messages: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group messages by thread_ts. Non-threaded messages are excluded.

    Pure function. Returns mapping of thread_ts -> list of messages.
    """
    threads: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        thread_ts = msg.get("thread_ts")
        if thread_ts:
            threads.setdefault(thread_ts, []).append(msg)
    return threads


def _is_thread_idle(
    thread_messages: list[dict[str, Any]],
    now: datetime,
    idle_seconds: int = _THREAD_IDLE_SECONDS,
) -> bool:
    """Check if a thread has been idle for longer than the threshold.

    Pure function. Compares latest message ts to current time.
    """
    if not thread_messages:
        return False

    latest_ts = max(
        float(m.get("ts", "0")) for m in thread_messages
    )
    last_activity = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
    return (now - last_activity).total_seconds() > idle_seconds


def _extract_participants(messages: list[dict[str, Any]]) -> list[str]:
    """Extract unique participant usernames/user_ids from thread messages.

    Pure function. Preserves insertion order, deduplicates.
    """
    seen: set[str] = set()
    participants: list[str] = []
    for msg in messages:
        name = msg.get("username") or msg.get("display_name") or msg.get("user_id", "")
        if name and name not in seen:
            seen.add(name)
            participants.append(name)
    return participants


def _build_thread_context(
    messages: list[dict[str, Any]], max_messages: int = _MAX_MESSAGES_PER_HAIKU_BATCH
) -> str:
    """Build a text block of thread messages for Haiku summarization.

    Pure function. Truncates to max_messages most recent.
    """
    sorted_msgs = sorted(messages, key=lambda m: m.get("ts", "0"))
    truncated = sorted_msgs[-max_messages:]
    lines = []
    for msg in truncated:
        name = msg.get("username") or msg.get("user_id", "unknown")
        text = msg.get("text", "")
        lines.append(f"{name}: {text}")
    return "\n".join(lines)


def _build_nightly_context(
    messages: list[dict[str, Any]], max_messages: int = _MAX_MESSAGES_PER_HAIKU_BATCH
) -> str:
    """Build a text block of day's messages for nightly Haiku analysis.

    Pure function. Truncates to max_messages.
    """
    truncated = messages[:max_messages]
    lines = []
    for msg in truncated:
        name = msg.get("username") or msg.get("user_id", "unknown")
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        lines.append(f"[{ts}] {name}: {text}")
    return "\n".join(lines)


def _build_thread_summary_record(
    channel_id: str,
    thread_ts: str,
    messages: list[dict[str, Any]],
    haiku_result: dict[str, Any],
) -> dict[str, Any]:
    """Build a thread summary JSONL record from Haiku output.

    Pure function.
    """
    return {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "message_count": len(messages),
        "participants": _extract_participants(messages),
        "summary": haiku_result.get("summary", ""),
        "action_items": haiku_result.get("action_items", []),
        "sentiment": haiku_result.get("sentiment", "neutral"),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Haiku invocation (side-effect boundary)
# ---------------------------------------------------------------------------


def _default_haiku_invoke(prompt: str) -> str:
    """Invoke Haiku via claude CLI subprocess.

    This is the production Haiku invocation boundary. In tests,
    this function is replaced with a mock.

    Returns raw text output from Haiku.
    """
    try:
        result = subprocess.run(
            [
                "claude",
                "--model", "haiku",
                "--print",
                "--max-turns", "1",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            log.error("Haiku invocation failed: %s", result.stderr)
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("Haiku invocation error: %s", e)
        return ""


def _parse_haiku_json(raw: str) -> dict[str, Any]:
    """Parse JSON from Haiku output, handling markdown fences.

    Attempts to extract JSON from the response, tolerating
    common LLM output artifacts like ```json fences.
    """
    text = raw.strip()

    # Strip markdown JSON fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Failed to parse Haiku output as JSON: %.100s...", text)
        return {}


# ---------------------------------------------------------------------------
# SlackIndexer
# ---------------------------------------------------------------------------


class SlackIndexer:
    """Async indexing pipeline for Slack message logs.

    Orchestrates three indexing layers:
    1. FTS5 keyword index (no LLM)
    2. Thread summaries (Haiku)
    3. Nightly topic clusters + action items (Haiku)

    Haiku invocation is injectable for testability.
    """

    def __init__(
        self,
        connector_root: Path | None = None,
        log_store: SlackLogStore | None = None,
        keyword_index: KeywordIndex | None = None,
        haiku_invoke: Callable[[str], str] | None = None,
    ) -> None:
        self._root = connector_root or _DEFAULT_CONNECTOR_ROOT
        self._log_store = log_store or SlackLogStore()
        self._keyword_index = keyword_index or KeywordIndex(
            state_dir=self._root / "state"
        )
        self._haiku_invoke = haiku_invoke or _default_haiku_invoke

        # Derived paths
        self._config_path = self._root / "config" / "channels.yaml"
        self._index_dir = self._root / "index"
        self._thread_summaries_dir = self._index_dir / "thread-summaries"
        self._topic_clusters_dir = self._index_dir / "topic-clusters"
        self._action_items_dir = self._index_dir / "action-items"

    def _channel_config(self) -> dict[str, dict[str, Any]]:
        """Load channel config. Cached per invocation only."""
        return _load_channel_config(self._config_path)

    # -------------------------------------------------------------------
    # Layer 1: Keyword index (no LLM)
    # -------------------------------------------------------------------

    def build_keyword_index(self, channel_id: str, date: str) -> int:
        """Populate FTS5 from JSONL logs. Incremental via cursor.

        Reads messages from the log store for the given channel/date,
        filters to only new messages (after the cursor), and indexes them.
        Updates the cursor on success.

        Returns count of messages indexed.
        """
        config = self._channel_config()
        if _is_channel_ignored(channel_id, config):
            log.debug("Skipping ignored channel %s", channel_id)
            return 0

        messages = self._log_store.query(channel_id, date)
        if not messages:
            return 0

        cursor_ts = self._keyword_index.get_cursor(channel_id)
        new_messages = _filter_after_cursor(messages, cursor_ts)

        if not new_messages:
            return 0

        count = self._keyword_index.index_messages(new_messages)

        new_cursor = _max_ts(new_messages)
        if new_cursor:
            self._keyword_index.set_cursor(channel_id, new_cursor)

        log.info(
            "Keyword-indexed %d messages for %s on %s", count, channel_id, date
        )
        return count

    def build_keyword_index_all(self, date: str) -> int:
        """Build keyword index for all known channels on a date.

        Convenience method that iterates over all channels in the log store.
        Returns total count indexed.
        """
        total = 0
        for channel_id in self._log_store.list_channels():
            total += self.build_keyword_index(channel_id, date)
        return total

    # -------------------------------------------------------------------
    # Layer 2: Thread summaries (Haiku)
    # -------------------------------------------------------------------

    def summarize_idle_threads(self, date: str | None = None) -> int:
        """Find threads idle >2h, summarize with Haiku, write JSONL.

        Scans all channels for threads where the last reply was >2h ago.
        Calls Haiku to produce a summary for each idle thread.
        Writes results to index/thread-summaries/{channel_id}/{date}.jsonl.

        Returns count of threads summarized.
        """
        if not _is_index_enabled():
            log.info("Slack indexing disabled; skipping thread summaries")
            return 0

        config = self._channel_config()
        now = datetime.now(timezone.utc)
        target_date = date or now.strftime("%Y-%m-%d")
        count = 0

        for channel_id in self._log_store.list_channels():
            if _is_channel_ignored(channel_id, config):
                continue

            messages = self._log_store.query(channel_id, target_date)
            threads = _group_by_thread(messages)

            for thread_ts, thread_msgs in threads.items():
                if not _is_thread_idle(thread_msgs, now):
                    continue

                # Skip if already summarized
                if self._thread_summary_exists(channel_id, thread_ts, target_date):
                    continue

                # Enforce batch limit
                if len(thread_msgs) > _MAX_MESSAGES_PER_HAIKU_BATCH:
                    thread_msgs = sorted(
                        thread_msgs, key=lambda m: m.get("ts", "0")
                    )[-_MAX_MESSAGES_PER_HAIKU_BATCH:]

                summary = self._summarize_thread(channel_id, thread_ts, thread_msgs)
                if summary:
                    self._write_thread_summary(channel_id, target_date, summary)
                    count += 1

        log.info("Summarized %d idle threads", count)
        return count

    def _summarize_thread(
        self,
        channel_id: str,
        thread_ts: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Invoke Haiku to summarize a single thread."""
        context = _build_thread_context(messages)
        prompt = textwrap.dedent(f"""\
            Summarize this Slack thread conversation. Return valid JSON with these fields:
            - "summary": A 1-3 sentence summary of what was discussed and decided.
            - "action_items": A list of strings, each describing a committed action (format: "person: action").
            - "sentiment": One of "positive", "negative", "neutral", "mixed".

            Thread messages:
            {context}

            Return ONLY valid JSON, no other text.
        """)

        raw = self._haiku_invoke(prompt)
        if not raw:
            return None

        result = _parse_haiku_json(raw)
        if not result:
            return None

        return _build_thread_summary_record(channel_id, thread_ts, messages, result)

    def _thread_summary_exists(
        self, channel_id: str, thread_ts: str, date: str
    ) -> bool:
        """Check if a thread summary already exists for deduplication."""
        summary_file = self._thread_summaries_dir / channel_id / f"{date}.jsonl"
        if not summary_file.exists():
            return False

        with open(summary_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("thread_ts") == thread_ts:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def _write_thread_summary(
        self, channel_id: str, date: str, summary: dict[str, Any]
    ) -> None:
        """Append a thread summary record to the JSONL output file."""
        out_dir = self._thread_summaries_dir / channel_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{date}.jsonl"

        with open(out_file, "a") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")

        log.debug(
            "Wrote thread summary for %s/%s to %s",
            channel_id,
            summary.get("thread_ts"),
            out_file,
        )

    # -------------------------------------------------------------------
    # Layer 3: Nightly topic clusters + action items (Haiku)
    # -------------------------------------------------------------------

    def run_nightly_index(self, channel_id: str, date: str) -> None:
        """Nightly analysis: topic clusters + action items for a channel/date.

        Uses Haiku to analyze the day's messages and produce:
        - topic-clusters/{channel_id}/{date}.json
        - action-items/{channel_id}/{date}.json

        Skipped if indexing is disabled or channel is ignored.
        """
        if not _is_index_enabled():
            log.info("Slack indexing disabled; skipping nightly index")
            return

        config = self._channel_config()
        if _is_channel_ignored(channel_id, config):
            log.debug("Skipping ignored channel %s", channel_id)
            return

        messages = self._log_store.query(channel_id, date)
        if not messages:
            log.debug("No messages for %s on %s", channel_id, date)
            return

        # Process in batches if needed
        batch = messages[:_MAX_MESSAGES_PER_HAIKU_BATCH]
        has_more = len(messages) > _MAX_MESSAGES_PER_HAIKU_BATCH

        context = _build_nightly_context(batch)

        # Topic clusters
        topic_result = self._analyze_topics(context)
        if topic_result:
            self._write_json_index(
                self._topic_clusters_dir, channel_id, date, topic_result
            )

        # Action items
        action_result = self._analyze_actions(context)
        if action_result:
            self._write_json_index(
                self._action_items_dir, channel_id, date, action_result
            )

        if has_more:
            remaining = len(messages) - _MAX_MESSAGES_PER_HAIKU_BATCH
            log.info(
                "Nightly index for %s/%s processed %d of %d messages; "
                "%d remaining for continuation",
                channel_id,
                date,
                len(batch),
                len(messages),
                remaining,
            )

    def run_nightly_index_all(self, date: str) -> None:
        """Run nightly index for all known channels."""
        if not _is_index_enabled():
            log.info("Slack indexing disabled; skipping nightly index")
            return

        for channel_id in self._log_store.list_channels():
            self.run_nightly_index(channel_id, date)

    def _analyze_topics(self, context: str) -> dict[str, Any] | None:
        """Invoke Haiku to extract topic clusters from day's messages."""
        prompt = textwrap.dedent(f"""\
            Analyze these Slack messages and identify topic clusters.
            Return valid JSON with:
            - "topics": A list of objects, each with:
              - "topic": Short topic label (2-5 words)
              - "message_count": How many messages relate to this topic
              - "key_points": List of 1-3 key points discussed
              - "participants": List of usernames involved

            Messages:
            {context}

            Return ONLY valid JSON, no other text.
        """)

        raw = self._haiku_invoke(prompt)
        if not raw:
            return None

        result = _parse_haiku_json(raw)
        if not result or "topics" not in result:
            return None

        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        return result

    def _analyze_actions(self, context: str) -> dict[str, Any] | None:
        """Invoke Haiku to extract action items from day's messages."""
        prompt = textwrap.dedent(f"""\
            Extract action items from these Slack messages.
            An action item is something someone committed to doing or was asked to do.
            Return valid JSON with:
            - "action_items": A list of objects, each with:
              - "assignee": Username of the person responsible
              - "action": What they committed to or were asked to do
              - "context": Brief context (which conversation, thread)
              - "urgency": "high", "medium", or "low"

            Messages:
            {context}

            Return ONLY valid JSON, no other text.
        """)

        raw = self._haiku_invoke(prompt)
        if not raw:
            return None

        result = _parse_haiku_json(raw)
        if not result or "action_items" not in result:
            return None

        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        return result

    def _write_json_index(
        self,
        base_dir: Path,
        channel_id: str,
        date: str,
        data: dict[str, Any],
    ) -> None:
        """Write a JSON index file to the appropriate directory."""
        out_dir = base_dir / channel_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{date}.json"

        with open(out_file, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        log.debug("Wrote index to %s", out_file)
