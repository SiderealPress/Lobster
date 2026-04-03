"""
Slack Connector — Rule-Based Trigger Engine.

Matches Slack events against TOML rule files and fires actions.
Hot-reloads rules from a watched directory without restart.

Design principles:
- Pure functions for all matching logic (event matching, keyword checks, template interpolation)
- Side effects isolated to fire_action() boundary and file-watching
- Immutable rule snapshots: reload() atomically swaps the entire rule set
- Rules are data (TOML files), not code — editable without restarting
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("slack-trigger-engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RULES_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "rules"

_SHELL_TIMEOUT_SECONDS = 30

VALID_ACTION_TYPES = frozenset({
    "lobster_task",
    "send_reply",
    "telegram_notify",
    "webhook",
    "shell",
})

VALID_EVENT_TYPES = frozenset({
    "message",
    "reaction_added",
    "app_mention",
    "file_shared",
    "slash_command",
})

VALID_KEYWORD_MODES = frozenset({"any", "all"})


# ---------------------------------------------------------------------------
# Pure functions — TOML parsing and validation
# ---------------------------------------------------------------------------


def parse_rule(raw: dict[str, Any], source_path: str = "") -> Optional[dict[str, Any]]:
    """Parse and validate a raw TOML dict into a normalized rule.

    Pure function: takes parsed TOML, returns structured rule or None on error.
    The source_path is stored for debugging but does not affect logic.
    """
    rule_section = raw.get("rule", {})
    trigger_section = raw.get("trigger", {})
    action_section = raw.get("action", {})

    name = rule_section.get("name", "")
    if not name:
        log.warning("Rule at %s has no name, skipping", source_path)
        return None

    enabled = rule_section.get("enabled", True)
    event = trigger_section.get("event", "message")

    if event not in VALID_EVENT_TYPES:
        log.warning(
            "Rule %r has invalid event type %r, skipping",
            name, event,
        )
        return None

    action_type = action_section.get("type", "")
    if action_type not in VALID_ACTION_TYPES:
        log.warning(
            "Rule %r has invalid action type %r, skipping",
            name, action_type,
        )
        return None

    keyword_mode = trigger_section.get("keyword_mode", "any")
    if keyword_mode not in VALID_KEYWORD_MODES:
        log.warning(
            "Rule %r has invalid keyword_mode %r, defaulting to 'any'",
            name, keyword_mode,
        )
        keyword_mode = "any"

    # Compile regex if provided
    regex_pattern = trigger_section.get("regex")
    compiled_regex = None
    if regex_pattern:
        try:
            compiled_regex = re.compile(regex_pattern, re.IGNORECASE)
        except re.error as e:
            log.warning("Rule %r has invalid regex %r: %s", name, regex_pattern, e)
            return None

    return {
        "name": name,
        "description": rule_section.get("description", ""),
        "enabled": bool(enabled),
        "source_path": source_path,
        # Trigger
        "event": event,
        "channels": list(trigger_section.get("channels", []) or []),
        "users": list(trigger_section.get("users", []) or []),
        "keywords": list(trigger_section.get("keywords", []) or []),
        "keyword_mode": keyword_mode,
        "emoji": trigger_section.get("emoji"),
        "command": trigger_section.get("command"),
        "file_type": trigger_section.get("file_type"),
        "regex": regex_pattern,
        "compiled_regex": compiled_regex,
        # Action
        "action_type": action_type,
        "action": dict(action_section),
    }


def load_rules_from_dir(rules_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all TOML rule files from a directory into a {name: rule} dict.

    Pure-ish: performs file I/O reads but produces an immutable snapshot.
    Skips invalid files gracefully.
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    rules: dict[str, dict[str, Any]] = {}

    if not rules_dir.exists():
        log.info("Rules directory %s does not exist, no rules loaded", rules_dir)
        return rules

    toml_files = sorted(rules_dir.glob("**/*.toml"))
    # Exclude files in examples/ subdirectory from auto-loading
    toml_files = [
        f for f in toml_files
        if "examples" not in f.relative_to(rules_dir).parts
    ]

    for path in toml_files:
        try:
            raw = tomllib.loads(path.read_text())
            rule = parse_rule(raw, source_path=str(path))
            if rule is not None:
                if rule["name"] in rules:
                    log.warning(
                        "Duplicate rule name %r (in %s), overwriting",
                        rule["name"], path,
                    )
                rules[rule["name"]] = rule
        except Exception:
            log.exception("Failed to parse rule file %s, skipping", path)

    log.info("Loaded %d rules from %s", len(rules), rules_dir)
    return rules


# ---------------------------------------------------------------------------
# Pure functions — event matching
# ---------------------------------------------------------------------------


def matches_event_type(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event type matches the rule's trigger event.

    Pure function.
    """
    event_type = event.get("type", event.get("event_type", "message"))
    return rule["event"] == event_type


def matches_channel(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event's channel matches the rule's channel filter.

    Pure function. Empty channels list matches all channels.
    """
    rule_channels = rule.get("channels", [])
    if not rule_channels:
        return True
    channel_id = event.get("channel", event.get("channel_id", ""))
    return channel_id in rule_channels


def matches_user(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event's user matches the rule's user filter.

    Pure function. Empty users list matches all users.
    """
    rule_users = rule.get("users", [])
    if not rule_users:
        return True
    user_id = event.get("user", event.get("user_id", ""))
    return user_id in rule_users


def matches_keywords(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event text matches the rule's keyword filter.

    Pure function.
    - No keywords configured: always matches
    - keyword_mode "any": at least one keyword present
    - keyword_mode "all": all keywords must be present
    Keywords are matched case-insensitively against the message text.
    """
    keywords = rule.get("keywords", [])
    if not keywords:
        return True

    text = event.get("text", "").lower()
    keyword_mode = rule.get("keyword_mode", "any")

    keyword_checks = (kw.lower() in text for kw in keywords)

    if keyword_mode == "all":
        return all(kw.lower() in text for kw in keywords)
    else:  # "any"
        return any(kw.lower() in text for kw in keywords)


def matches_emoji(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event's reaction emoji matches the rule's emoji filter.

    Pure function. None emoji filter matches all.
    """
    rule_emoji = rule.get("emoji")
    if rule_emoji is None:
        return True
    event_emoji = event.get("reaction", event.get("emoji", ""))
    return event_emoji == rule_emoji


def matches_command(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event's slash command matches the rule's command filter.

    Pure function. None command filter matches all.
    """
    rule_command = rule.get("command")
    if rule_command is None:
        return True
    event_command = event.get("command", "")
    return event_command == rule_command


def matches_file_type(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event's file type matches the rule's file_type filter.

    Pure function. None file_type filter matches all.
    """
    rule_file_type = rule.get("file_type")
    if rule_file_type is None:
        return True
    # Check files array or top-level file_type
    files = event.get("files", [])
    if files:
        return any(f.get("filetype", f.get("mimetype", "")) == rule_file_type for f in files)
    return event.get("filetype", "") == rule_file_type


def matches_regex(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Check if the event text matches the rule's regex pattern.

    Pure function. None regex matches all.
    """
    compiled = rule.get("compiled_regex")
    if compiled is None:
        return True
    text = event.get("text", "")
    return compiled.search(text) is not None


def evaluate_rule(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    """Evaluate whether a single rule matches an event.

    Pure function. Composes all individual matchers via conjunction (AND).
    A rule matches only when ALL of its configured predicates pass.
    """
    if not rule.get("enabled", True):
        return False

    matchers = (
        matches_event_type,
        matches_channel,
        matches_user,
        matches_keywords,
        matches_emoji,
        matches_command,
        matches_file_type,
        matches_regex,
    )

    return all(matcher(rule, event) for matcher in matchers)


def evaluate_all(
    rules: dict[str, dict[str, Any]], event: dict[str, Any]
) -> list[dict[str, Any]]:
    """Evaluate all rules against an event. Returns matched rules.

    Pure function.
    """
    return [rule for rule in rules.values() if evaluate_rule(rule, event)]


# ---------------------------------------------------------------------------
# Pure functions — template variable interpolation
# ---------------------------------------------------------------------------


def build_template_vars(event: dict[str, Any]) -> dict[str, str]:
    """Build a template variable dict from an event.

    Pure function. Returns {var_name: value} for all known template variables.
    """
    now = datetime.now(timezone.utc)

    return {
        "message_text": event.get("text", ""),
        "channel_id": event.get("channel", event.get("channel_id", "")),
        "channel_name": event.get("channel_name", ""),
        "user_id": event.get("user", event.get("user_id", "")),
        "username": event.get("username", ""),
        "ts": event.get("ts", ""),
        "thread_ts": event.get("thread_ts", ""),
        "emoji": event.get("reaction", event.get("emoji", "")),
        "original_message_text": _extract_original_message_text(event),
        "command_text": event.get("text", event.get("command_text", "")),
        "file_name": _extract_file_name(event),
        "date": now.strftime("%Y-%m-%d"),
    }


def _extract_original_message_text(event: dict[str, Any]) -> str:
    """Extract original message text from reaction or thread events.

    Pure function.
    """
    item = event.get("item", {})
    if isinstance(item, dict):
        return item.get("text", "")
    return ""


def _extract_file_name(event: dict[str, Any]) -> str:
    """Extract the first file name from an event.

    Pure function.
    """
    files = event.get("files", [])
    if files and isinstance(files[0], dict):
        return files[0].get("name", "")
    return event.get("file_name", "")


def interpolate_template(template: str, variables: dict[str, str]) -> str:
    """Replace {var_name} placeholders in a template string.

    Pure function. Unknown variables are left as-is.
    Uses a safe approach that only replaces known variable patterns.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


# ---------------------------------------------------------------------------
# TriggerEngine — stateful orchestrator with hot-reload
# ---------------------------------------------------------------------------


class TriggerEngine:
    """Rule-based trigger engine for Slack events.

    State is limited to the loaded rules snapshot and the optional
    file watcher. All matching and interpolation logic delegates
    to pure functions above.
    """

    def __init__(self, rules_dir: Optional[str] = None) -> None:
        self._rules_dir = Path(rules_dir) if rules_dir else _DEFAULT_RULES_DIR
        self._rules: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_stop = threading.Event()
        self.reload_rules()

    @property
    def rules(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of the current rules. Thread-safe read."""
        with self._lock:
            return dict(self._rules)

    @property
    def rule_count(self) -> int:
        """Number of currently loaded rules."""
        with self._lock:
            return len(self._rules)

    def evaluate(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Match event against all loaded rules. Returns list of matched rules.

        Thread-safe: reads a snapshot of rules under lock.
        """
        with self._lock:
            rules_snapshot = dict(self._rules)
        return evaluate_all(rules_snapshot, event)

    def fire_action(self, rule: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """Execute the action defined in a matched rule.

        Side-effect boundary: this is where I/O happens (subprocess, HTTP, etc.).
        Returns a result dict with status and any output/error.
        """
        action_type = rule.get("action_type", "")
        action = rule.get("action", {})
        template_vars = build_template_vars(event)

        handler = _ACTION_HANDLERS.get(action_type)
        if handler is None:
            return {"status": "error", "error": f"Unknown action type: {action_type}"}

        try:
            return handler(action, template_vars)
        except Exception as e:
            log.exception("Error firing action for rule %r", rule.get("name", "?"))
            return {"status": "error", "error": str(e)}

    def reload_rules(self) -> None:
        """Re-read all TOML files from rules_dir. Atomic swap under lock."""
        new_rules = load_rules_from_dir(self._rules_dir)
        with self._lock:
            self._rules = new_rules

    def watch_rules_dir(self) -> None:
        """Start a background thread that watches rules_dir for changes.

        Uses watchdog for inotify-based file watching. On any TOML file
        change (create, modify, delete), triggers reload_rules().
        """
        if self._watcher_thread is not None and self._watcher_thread.is_alive():
            log.warning("Watcher already running")
            return

        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._run_watcher,
            daemon=True,
            name="trigger-engine-watcher",
        )
        self._watcher_thread.start()
        log.info("Started rules directory watcher for %s", self._rules_dir)

    def stop_watcher(self) -> None:
        """Stop the background file watcher."""
        self._watcher_stop.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=5)
            self._watcher_thread = None
            log.info("Stopped rules directory watcher")

    def _run_watcher(self) -> None:
        """Background watcher loop using watchdog."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileSystemEvent

            engine = self

            class _RuleFileHandler(FileSystemEventHandler):
                """Handles file system events for TOML rule files."""

                def on_any_event(self, event: FileSystemEvent) -> None:
                    if event.is_directory:
                        return
                    src_path = str(event.src_path)
                    if not src_path.endswith(".toml"):
                        return
                    # Skip example files
                    if "/examples/" in src_path:
                        return
                    log.info(
                        "Rule file change detected (%s): %s",
                        event.event_type,
                        src_path,
                    )
                    engine.reload_rules()

            self._rules_dir.mkdir(parents=True, exist_ok=True)
            observer = Observer()
            observer.schedule(_RuleFileHandler(), str(self._rules_dir), recursive=True)
            observer.start()

            try:
                while not self._watcher_stop.wait(timeout=1.0):
                    pass
            finally:
                observer.stop()
                observer.join(timeout=5)

        except ImportError:
            log.error(
                "watchdog library not available — hot-reload disabled. "
                "Install with: uv add watchdog"
            )
        except Exception:
            log.exception("Rules directory watcher crashed")


# ---------------------------------------------------------------------------
# Action handlers — each performs one type of side effect
# ---------------------------------------------------------------------------


def _handle_lobster_task(
    action: dict[str, Any], template_vars: dict[str, str]
) -> dict[str, Any]:
    """Spawn a Claude subprocess with an interpolated task prompt.

    Side effect: spawns a subprocess.
    """
    task_prompt = action.get("task_prompt", "")
    if not task_prompt:
        return {"status": "error", "error": "lobster_task action missing task_prompt"}

    prompt = interpolate_template(task_prompt, template_vars)
    subagent_type = action.get("subagent_type", "general-purpose")
    run_in_background = action.get("run_in_background", True)

    log.info(
        "Firing lobster_task: subagent_type=%s, background=%s, prompt_length=%d",
        subagent_type, run_in_background, len(prompt),
    )

    return {
        "status": "pending",
        "action_type": "lobster_task",
        "task_prompt": prompt,
        "subagent_type": subagent_type,
        "run_in_background": run_in_background,
    }


def _handle_send_reply(
    action: dict[str, Any], template_vars: dict[str, str]
) -> dict[str, Any]:
    """Post a static message to a Slack channel.

    Returns the interpolated message for the caller to dispatch.
    """
    message = action.get("message", action.get("text", ""))
    if not message:
        return {"status": "error", "error": "send_reply action missing message"}

    channel = action.get("channel", template_vars.get("channel_id", ""))
    interpolated = interpolate_template(message, template_vars)

    return {
        "status": "ready",
        "action_type": "send_reply",
        "channel": channel,
        "message": interpolated,
    }


def _handle_telegram_notify(
    action: dict[str, Any], template_vars: dict[str, str]
) -> dict[str, Any]:
    """Send a notification to the admin via Telegram.

    Returns the interpolated message for the caller to dispatch.
    """
    message = action.get("message", action.get("text", ""))
    if not message:
        return {"status": "error", "error": "telegram_notify action missing message"}

    chat_id = action.get("chat_id", os.environ.get("LOBSTER_ADMIN_CHAT_ID", ""))
    interpolated = interpolate_template(message, template_vars)

    return {
        "status": "ready",
        "action_type": "telegram_notify",
        "chat_id": chat_id,
        "message": interpolated,
    }


def _handle_webhook(
    action: dict[str, Any], template_vars: dict[str, str]
) -> dict[str, Any]:
    """HTTP POST to a URL with JSON event payload.

    Side effect: makes HTTP request.
    """
    url = action.get("url", "")
    if not url:
        return {"status": "error", "error": "webhook action missing url"}

    import json
    import urllib.request
    import urllib.error

    payload = {
        "rule_triggered": True,
        "variables": template_vars,
    }

    # Allow custom body template
    body_template = action.get("body")
    if body_template:
        interpolated_body = interpolate_template(body_template, template_vars)
        try:
            payload = json.loads(interpolated_body)
        except json.JSONDecodeError:
            payload = {"text": interpolated_body, "variables": template_vars}

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    # Merge custom headers
    custom_headers = action.get("headers", {})
    if isinstance(custom_headers, dict):
        headers.update(custom_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {
                "status": "success",
                "action_type": "webhook",
                "url": url,
                "response_code": resp.status,
            }
    except urllib.error.URLError as e:
        return {
            "status": "error",
            "action_type": "webhook",
            "url": url,
            "error": str(e),
        }


def _handle_shell(
    action: dict[str, Any], template_vars: dict[str, str]
) -> dict[str, Any]:
    """Run a shell command with timeout.

    Side effect: spawns subprocess.
    """
    command = action.get("command", "")
    if not command:
        return {"status": "error", "error": "shell action missing command"}

    interpolated = interpolate_template(command, template_vars)
    timeout = min(action.get("timeout", _SHELL_TIMEOUT_SECONDS), _SHELL_TIMEOUT_SECONDS)

    try:
        result = subprocess.run(
            interpolated,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "action_type": "shell",
            "command": interpolated,
            "returncode": result.returncode,
            "stdout": result.stdout[:1000],  # Truncate for safety
            "stderr": result.stderr[:1000],
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "action_type": "shell",
            "command": interpolated,
            "error": f"Command timed out after {timeout}s",
        }


# Handler dispatch table — maps action type string to handler function
_ACTION_HANDLERS: dict[str, Any] = {
    "lobster_task": _handle_lobster_task,
    "send_reply": _handle_send_reply,
    "telegram_notify": _handle_telegram_notify,
    "webhook": _handle_webhook,
    "shell": _handle_shell,
}
