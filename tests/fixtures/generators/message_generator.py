"""
Lobster Test Message Generator

Generates test messages programmatically for unit and stress tests.
"""

import json
import random
import string
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class MessageGenerator:
    """Generate test messages for Lobster testing."""

    # Sample user names for variety
    SAMPLE_USERS = [
        ("alice", "Alice", 100001),
        ("bob", "Bob", 100002),
        ("charlie", "Charlie", 100003),
        ("diana", "Diana", 100004),
        ("eve", "Eve", 100005),
    ]

    # Sample message templates
    SAMPLE_TEXTS = [
        "Hello, how are you?",
        "Can you help me with something?",
        "What's the weather like today?",
        "Remind me to call mom at 5pm",
        "What time is it?",
        "Tell me a joke",
        "Set a timer for 10 minutes",
        "What's on my schedule today?",
        "Search for Python tutorials",
        "Translate 'hello' to Spanish",
    ]

    def __init__(self, seed: Optional[int] = None):
        """Initialize generator with optional random seed for reproducibility."""
        if seed is not None:
            random.seed(seed)
        self._counter = 0

    def generate_text_message(
        self,
        text: Optional[str] = None,
        source: str = "telegram",
        user_name: Optional[str] = None,
        username: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> dict:
        """
        Generate a single text message.

        Args:
            text: Message text (random if not provided)
            source: Message source (telegram, sms, signal)
            user_name: Display name
            username: @username
            user_id: Numeric user ID
            chat_id: Chat ID for replies
            message_id: Unique message ID
            timestamp: ISO timestamp

        Returns:
            Message dict ready for JSON serialization
        """
        self._counter += 1

        # Select random user if not provided
        if user_name is None or username is None or user_id is None:
            username_sample, name_sample, id_sample = random.choice(self.SAMPLE_USERS)
            user_name = user_name or name_sample
            username = username or username_sample
            user_id = user_id or id_sample

        # Generate defaults
        if text is None:
            text = random.choice(self.SAMPLE_TEXTS)

        if chat_id is None:
            chat_id = user_id  # For Telegram, chat_id often equals user_id in DMs

        if message_id is None:
            ts_ms = int(time.time() * 1000)
            message_id = f"{ts_ms}_{self._counter}"

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        return {
            "id": message_id,
            "source": source,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "user_name": user_name,
            "text": text,
            "type": "text",
            "timestamp": timestamp,
        }

    def generate_voice_message(
        self,
        audio_file: Optional[str] = None,
        duration: int = 5,
        source: str = "telegram",
        user_name: Optional[str] = None,
        username: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        transcription: Optional[str] = None,
    ) -> dict:
        """
        Generate a voice message.

        Args:
            audio_file: Path to audio file
            duration: Audio duration in seconds
            source: Message source
            user_name: Display name
            username: @username
            user_id: Numeric user ID
            chat_id: Chat ID for replies
            message_id: Unique message ID
            timestamp: ISO timestamp
            transcription: Pre-existing transcription (if any)

        Returns:
            Voice message dict
        """
        # Start with a text message base
        msg = self.generate_text_message(
            text="[Voice message - pending transcription]",
            source=source,
            user_name=user_name,
            username=username,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            timestamp=timestamp,
        )

        # Add voice-specific fields
        msg["type"] = "voice"
        msg["audio_file"] = audio_file or f"/tmp/audio_{msg['id']}.ogg"
        msg["audio_duration"] = duration
        msg["audio_mime_type"] = "audio/ogg"
        msg["file_id"] = f"voice_{uuid.uuid4().hex[:16]}"

        if transcription:
            msg["transcription"] = transcription
            msg["text"] = transcription

        return msg

    def generate_batch(
        self,
        count: int,
        source: str = "telegram",
        include_voice: bool = False,
        voice_ratio: float = 0.1,
        **kwargs,
    ) -> list[dict]:
        """
        Generate a batch of messages.

        Args:
            count: Number of messages to generate
            source: Message source
            include_voice: Whether to include voice messages
            voice_ratio: Ratio of voice messages (0.0-1.0)
            **kwargs: Additional arguments passed to message generators

        Returns:
            List of message dicts
        """
        messages = []
        for i in range(count):
            if include_voice and random.random() < voice_ratio:
                msg = self.generate_voice_message(source=source, **kwargs)
            else:
                msg = self.generate_text_message(source=source, **kwargs)
            messages.append(msg)
        return messages

    def generate_stress_batch(
        self,
        count: int = 1000,
        sources: Optional[list[str]] = None,
        include_voice: bool = True,
    ) -> list[dict]:
        """
        Generate a large batch of varied messages for stress testing.

        Args:
            count: Number of messages (default 1000)
            sources: List of sources to use (default: telegram)
            include_voice: Include voice messages

        Returns:
            List of varied message dicts
        """
        if sources is None:
            sources = ["telegram"]

        messages = []
        for i in range(count):
            source = random.choice(sources)

            # Vary the content
            text_variants = [
                random.choice(self.SAMPLE_TEXTS),
                f"Message number {i}",
                "".join(random.choices(string.ascii_letters + " ", k=random.randint(10, 200))),
            ]

            msg = self.generate_text_message(
                text=random.choice(text_variants),
                source=source,
            )

            # Occasionally make it a voice message
            if include_voice and random.random() < 0.05:
                msg["type"] = "voice"
                msg["audio_file"] = f"/tmp/stress_audio_{i}.ogg"
                msg["audio_duration"] = random.randint(1, 120)
                msg["text"] = "[Voice message - pending transcription]"

            messages.append(msg)

        return messages

    def generate_edge_case_messages(self) -> list[dict]:
        """
        Generate messages with edge cases for testing.

        Returns:
            List of edge case message dicts
        """
        edge_cases = []

        # Unicode/emoji heavy
        edge_cases.append(
            self.generate_text_message(
                text="Hello! \U0001f600 \U0001f389 \U0001f680 Unicode test: \u4e2d\u6587 \u0420\u0443\u0441\u0441\u043a\u0438\u0439 \u05e2\u05d1\u05e8\u05d9\u05ea",
            )
        )

        # Very long text
        edge_cases.append(
            self.generate_text_message(
                text="x" * 10000,  # 10KB message
            )
        )

        # Empty-ish text
        edge_cases.append(
            self.generate_text_message(
                text="   ",  # Just whitespace
            )
        )

        # Special characters
        edge_cases.append(
            self.generate_text_message(
                text='Special chars: <script>alert("xss")</script> && || ; ` $ {} [] " \'',
            )
        )

        # Newlines and formatting
        edge_cases.append(
            self.generate_text_message(
                text="Line 1\nLine 2\n\nLine 4\r\nWindows line\tTab here",
            )
        )

        # JSON-like content
        edge_cases.append(
            self.generate_text_message(
                text='{"key": "value", "nested": {"array": [1, 2, 3]}}',
            )
        )

        # URL content
        edge_cases.append(
            self.generate_text_message(
                text="Check this: https://example.com/path?param=value&other=123#anchor",
            )
        )

        # Markdown-like content
        edge_cases.append(
            self.generate_text_message(
                text="**Bold** _italic_ `code` [link](url) # Header",
            )
        )

        return edge_cases

    @staticmethod
    def save_fixtures(messages: list[dict], path: Path) -> None:
        """
        Save messages to a JSON fixture file.

        Args:
            messages: List of message dicts
            path: Path to save fixture file
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(messages, f, indent=2)

    @staticmethod
    def load_fixtures(path: Path) -> list[dict]:
        """
        Load messages from a JSON fixture file.

        Args:
            path: Path to fixture file

        Returns:
            List of message dicts
        """
        with open(path, "r") as f:
            return json.load(f)


class TaskGenerator:
    """Generate test tasks for Lobster testing."""

    SAMPLE_SUBJECTS = [
        "Review pull request #123",
        "Update documentation",
        "Fix login bug",
        "Add unit tests",
        "Refactor database layer",
        "Deploy to staging",
        "Call client about project",
        "Research new framework",
        "Write blog post",
        "Schedule team meeting",
    ]

    def __init__(self, seed: Optional[int] = None):
        if seed is not None:
            random.seed(seed)
        self._next_id = 1

    def generate_task(
        self,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "pending",
        task_id: Optional[int] = None,
    ) -> dict:
        """Generate a single task."""
        if task_id is None:
            task_id = self._next_id
            self._next_id += 1

        if subject is None:
            subject = random.choice(self.SAMPLE_SUBJECTS)

        if description is None:
            description = f"Detailed description for: {subject}"

        now = datetime.now(timezone.utc).isoformat()

        return {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }

    def generate_batch(self, count: int, **kwargs) -> list[dict]:
        """Generate a batch of tasks."""
        return [self.generate_task(**kwargs) for _ in range(count)]


class ScheduledJobGenerator:
    """Generate test scheduled jobs for Lobster testing."""

    SAMPLE_JOBS = [
        ("morning-news", "0 8 * * *", "Fetch and summarize morning news"),
        ("daily-backup", "0 2 * * *", "Run daily backup script"),
        ("hourly-check", "0 * * * *", "Check system health"),
        ("weekly-report", "0 9 * * 1", "Generate weekly report"),
        ("cleanup-logs", "30 3 * * *", "Clean up old log files"),
    ]

    def generate_job(
        self,
        name: Optional[str] = None,
        schedule: Optional[str] = None,
        context: Optional[str] = None,
        enabled: bool = True,
    ) -> dict:
        """Generate a scheduled job."""
        if name is None or schedule is None or context is None:
            sample = random.choice(self.SAMPLE_JOBS)
            name = name or sample[0]
            schedule = schedule or sample[1]
            context = context or sample[2]

        now = datetime.now(timezone.utc).isoformat()

        return {
            "name": name,
            "schedule": schedule,
            "schedule_human": f"Cron: {schedule}",
            "task_file": f"tasks/{name}.md",
            "created_at": now,
            "updated_at": now,
            "enabled": enabled,
            "last_run": None,
            "last_status": None,
        }


# Convenience function for quick fixture generation
def generate_default_fixtures(output_dir: Path) -> None:
    """
    Generate all default fixtures for the test suite.

    Args:
        output_dir: Base directory for fixtures
    """
    msg_gen = MessageGenerator(seed=42)  # Reproducible
    task_gen = TaskGenerator(seed=42)
    job_gen = ScheduledJobGenerator()

    # Text messages
    text_messages = msg_gen.generate_batch(15)
    MessageGenerator.save_fixtures(
        text_messages,
        output_dir / "messages" / "text_messages.json",
    )

    # Voice messages
    voice_messages = [
        msg_gen.generate_voice_message(duration=5),
        msg_gen.generate_voice_message(duration=30),
        msg_gen.generate_voice_message(duration=120),
    ]
    MessageGenerator.save_fixtures(
        voice_messages,
        output_dir / "messages" / "voice_messages.json",
    )

    # Edge cases
    edge_cases = msg_gen.generate_edge_case_messages()
    MessageGenerator.save_fixtures(
        edge_cases,
        output_dir / "messages" / "edge_cases.json",
    )

    # Stress messages
    stress_messages = msg_gen.generate_stress_batch(1000)
    MessageGenerator.save_fixtures(
        stress_messages,
        output_dir / "messages" / "stress_messages.json",
    )

    # Tasks
    tasks = task_gen.generate_batch(10)
    task_data = {"tasks": tasks, "next_id": 11}
    with open(output_dir / "tasks" / "sample_tasks.json", "w") as f:
        json.dump(task_data, f, indent=2)

    # Scheduled jobs
    jobs = {job_gen.generate_job()["name"]: job_gen.generate_job() for _ in range(5)}
    job_data = {"jobs": jobs}
    with open(output_dir / "tasks" / "scheduled_jobs.json", "w") as f:
        json.dump(job_data, f, indent=2)


if __name__ == "__main__":
    # Generate fixtures when run directly
    import sys

    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent.parent

    print(f"Generating fixtures in {output_dir}")
    generate_default_fixtures(output_dir)
    print("Done!")
