"""
Live end-to-end test for the BIS-744 push-token confirmation flow.

Closes the one gap flagged by an independent (Fable) triple-check review of
PR #2131: every existing test for ``_queue_push_confirmation`` / the push
endpoints (``test_push_token_confirmation_shared.py``,
``test_push_calendar_token_confirmation.py``, etc.) either calls the helper
function directly in-process, or drives the ASGI app in-process via
Starlette's ``TestClient`` (an in-memory ASGI transport -- no real socket,
no real subprocess). Both are legitimate unit tests, but neither exercises
the actual deployed artifact: a real ``python src/mcp/inbox_server_http.py``
process, started the same way systemd starts it in production
(``lobster-mcp.service``), accepting a real TCP connection, parsing a real
HTTP request off the wire, and writing a real file to a real outbox
directory that a separate process (``lobster_bot.py``) actually watches.

This module closes that gap:

1. Spawns the real entrypoint (``src/mcp/inbox_server_http.py --port
   <free-port>``) as a genuine OS subprocess, using the SAME interpreter
   running the test suite (``sys.executable`` -- the project venv, same one
   ``lobster-mcp.service`` uses), pointed at an isolated ``LOBSTER_MESSAGES``
   tmp directory so it never touches the real production mailbox.
2. Drives it with real HTTP requests (the ``requests`` library, a real
   socket over 127.0.0.1) -- not an in-process ASGI transport.
3. Asserts on the real file that lands in the real (temp) outbox directory:
   presence, and that its schema matches exactly what
   ``src/bot/lobster_bot.py``'s ``OutboxHandler.process_reply()`` consumes
   (``id``, ``source``, ``chat_id``, ``text``, ``timestamp``).
4. Exercises the de-dupe guard (second identical push is a no-op) and the
   "failure must not claim the de-dupe slot" regression (a forced outbox
   failure, followed by a real retry once the failure is cleared) -- both
   over the real HTTP surface, by manipulating the real filesystem the live
   subprocess is reading and writing, not by patching Python objects inside
   it (you can't patch globals inside a separate OS process).

How "live" this actually is
----------------------------
- Real: OS-level subprocess boundary, real uvicorn/ASGI HTTP stack, real
  TCP socket, real bearer-token auth path, real file writes/reads, and (for
  the calendar scope used here) a REAL outbound HTTPS call from
  ``_fetch_calendar_preview`` -> ``get_upcoming_events`` -> Google's Calendar
  API (which legitimately 401s against the fake token used here and is
  handled by the existing graceful-degradation path -- this is a real
  network round trip, not a mock).
- Not covered here (still a gap, called out explicitly): this test does not
  exercise ``lobster_bot.py``'s ``OutboxHandler.process_reply()`` itself
  (which would require a real Telegram bot token and would actually send a
  Telegram message), and it targets a throwaway isolated MCP server instance
  rather than the literal singleton ``lobster-mcp.service`` process, since
  POSTing fabricated OAuth tokens at the production instance would write
  bogus token files into the real ``~/messages`` tree for whatever chat_id
  is used and could trigger a real outbound Telegram send. Schema
  conformance with ``process_reply()``'s expectations is asserted directly
  instead (see ``_assert_matches_lobster_bot_outbox_schema``).

Run just this file:
    make test-file FILE=tests/integration/test_push_confirmation_live_e2e.py
or:
    .venv/bin/python -m pytest tests/integration/test_push_confirmation_live_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
import requests

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_SCRIPT = _REPO_ROOT / "src" / "mcp" / "inbox_server_http.py"

_STARTUP_TIMEOUT_SECONDS = 15.0
_REQUEST_TIMEOUT_SECONDS = 20.0  # generous: _fetch_calendar_preview makes a real HTTPS call


def _free_port() -> int:
    """Ask the OS for a currently-unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _future_iso(hours: int = 1) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


class LiveServer:
    """A real ``inbox_server_http.py`` subprocess, isolated from production."""

    def __init__(self, base_url: str, proc: subprocess.Popen, dirs: dict[str, Path], log_path: Path):
        self.base_url = base_url
        self.proc = proc
        self.dirs = dirs
        self.log_path = log_path

    def post(self, path: str, *, json_body: dict, secret: str) -> requests.Response:
        return requests.post(
            f"{self.base_url}{path}",
            json=json_body,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    def server_log(self) -> str:
        try:
            return self.log_path.read_text()
        except OSError:
            return "<no log captured>"


@pytest.fixture(scope="module")
def live_server(tmp_path_factory) -> Iterator[LiveServer]:
    """Launch the real MCP HTTP server entrypoint as a genuine subprocess.

    Isolated from production via LOBSTER_MESSAGES pointed at a throwaway
    tmp directory -- never touches ~/messages. Uses sys.executable (the
    same interpreter running this test suite) rather than a hardcoded path,
    so this works from any worktree/venv, not just the production checkout.
    """
    assert _SERVER_SCRIPT.exists(), f"entrypoint not found: {_SERVER_SCRIPT}"

    base = tmp_path_factory.mktemp("live_e2e_messages")
    outbox_dir = base / "outbox"
    inbox_dir = base / "inbox"
    gcal_token_dir = base / "config" / "gcal-tokens"
    for d in (outbox_dir, inbox_dir, gcal_token_dir):
        d.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    log_path = base / "server.log"

    env = {
        # Deliberately NOT inheriting the real process environment's
        # LOBSTER_* values (if any) -- build a minimal, explicit env so this
        # test can never accidentally point at production config.
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LOBSTER_MESSAGES": str(base),
        "MCP_HTTP_TOKEN": "e2e-test-mcp-http-token",
        "LOBSTER_INTERNAL_SECRET": "e2e-test-internal-secret",
        # Explicitly warn-only (matches production default) so a fabricated
        # push without a signed session_token is accepted, not 401'd.
        "CALENDAR_PUSH_SIGNED_SESSION_ENFORCE": "false",
    }

    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(_SERVER_SCRIPT), "--port", str(port)],
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    last_error: Exception | None = None
    up = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_text = log_path.read_text() if log_path.exists() else "<no log>"
            raise RuntimeError(
                f"live_server subprocess exited early (code={proc.returncode}). "
                f"Log:\n{log_text}"
            )
        try:
            resp = requests.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code in (200, 503):
                up = True
                break
        except requests.exceptions.RequestException as exc:  # noqa: PERF203
            last_error = exc
        time.sleep(0.2)

    if not up:
        proc.terminate()
        proc.wait(timeout=5)
        log_text = log_path.read_text() if log_path.exists() else "<no log>"
        raise RuntimeError(
            f"live_server did not become healthy within "
            f"{_STARTUP_TIMEOUT_SECONDS}s (last_error={last_error}). Log:\n{log_text}"
        )

    server = LiveServer(
        base_url=base_url,
        proc=proc,
        dirs={"base": base, "outbox": outbox_dir, "inbox": inbox_dir, "gcal_tokens": gcal_token_dir},
        log_path=log_path,
    )
    try:
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _assert_matches_lobster_bot_outbox_schema(reply: dict, *, expected_chat_id: str) -> None:
    """Pin the exact fields ``lobster_bot.py``'s ``OutboxHandler.process_reply``
    (src/bot/lobster_bot.py) reads off an outbox JSON file: ``id`` (used to
    derive the temp/processed filenames), ``source`` (must be "telegram" or
    the router skips the file as belonging to another channel), ``chat_id``
    (the Telegram chat to send to), ``text`` (the message body), and
    ``timestamp``. A confirmation file missing or mistyping any of these
    would silently fail to deliver, or deliver to the wrong chat.
    """
    for field in ("id", "source", "chat_id", "text", "timestamp"):
        assert field in reply, f"outbox file missing required field {field!r}: {reply}"

    assert isinstance(reply["id"], str) and reply["id"], "id must be a non-empty string"
    assert reply["source"] == "telegram", "process_reply skips non-telegram outbox files"
    assert reply["chat_id"] == expected_chat_id
    assert isinstance(reply["text"], str) and reply["text"].strip()
    # timestamp must be a real parseable ISO 8601 string.
    datetime.fromisoformat(reply["timestamp"])


class TestLivePushConfirmationHappyPath:
    def test_real_http_push_writes_real_outbox_file_with_correct_schema(self, live_server):
        chat_id = "e2e_live_happy_1"
        resp = live_server.post(
            "/api/push-calendar-token",
            json_body={
                "chat_id": chat_id,
                "access_token": "ya29.e2e-fake-access-token",
                "refresh_token": "1//e2e-fake-refresh-token",
                "expires_at": _future_iso(),
                "scope": "https://www.googleapis.com/auth/calendar.events",
            },
            secret="e2e-test-internal-secret",
        )

        assert resp.status_code == 200, live_server.server_log()
        assert resp.json().get("ok") is True

        outbox_files = list(live_server.dirs["outbox"].glob(f"*{chat_id}*.json"))
        assert len(outbox_files) == 1, (
            f"expected exactly one outbox file for {chat_id!r}, "
            f"found {outbox_files}. Log:\n{live_server.server_log()}"
        )
        reply = json.loads(outbox_files[0].read_text())
        _assert_matches_lobster_bot_outbox_schema(reply, expected_chat_id=chat_id)
        assert "Calendar" in reply["text"]

        # Token itself must also have actually been persisted to disk by the
        # real endpoint (independent of the confirmation step).
        token_path = live_server.dirs["gcal_tokens"] / f"{chat_id}.json"
        assert token_path.exists()

    def test_wrong_bearer_token_is_rejected_by_the_real_server(self, live_server):
        """Sanity check that auth is real -- the live process, not a mock,
        is the one deciding 401 vs 200."""
        resp = live_server.post(
            "/api/push-calendar-token",
            json_body={
                "chat_id": "e2e_live_unauthorized",
                "access_token": "x",
                "refresh_token": "y",
                "expires_at": _future_iso(),
                "scope": "calendar",
            },
            secret="totally-wrong-secret",
        )
        assert resp.status_code == 401


class TestLivePushConfirmationDedupe:
    def test_second_identical_push_does_not_double_send(self, live_server):
        chat_id = "e2e_live_dedupe_1"
        body = {
            "chat_id": chat_id,
            "access_token": "ya29.e2e-fake",
            "refresh_token": "1//e2e-fake",
            "expires_at": _future_iso(),
            "scope": "https://www.googleapis.com/auth/calendar.events",
        }

        first = live_server.post(
            "/api/push-calendar-token", json_body=body, secret="e2e-test-internal-secret"
        )
        second = live_server.post(
            "/api/push-calendar-token", json_body=body, secret="e2e-test-internal-secret"
        )

        assert first.status_code == 200
        assert second.status_code == 200

        outbox_files = list(live_server.dirs["outbox"].glob(f"*{chat_id}*.json"))
        assert len(outbox_files) == 1, (
            f"a duplicate push over the real HTTP surface must not queue a "
            f"second confirmation; found {outbox_files}"
        )


class TestLivePushConfirmationForcedFailureIsRetryable:
    def test_forced_outbox_failure_does_not_claim_dedupe_slot_and_retry_succeeds(
        self, live_server
    ):
        """Regression test (the specific bug an earlier Fable review caught
        before merge): a transient outbox-write failure must NOT permanently
        block a legitimate retry (e.g. a webhook redelivery) for the rest of
        the de-dupe TTL window.

        Here the real outbox directory the LIVE subprocess is writing to is
        replaced with a blocking file (from this test process, on the real
        shared filesystem -- no patching of anything inside the subprocess
        is possible or needed). The confirmation write must fail loudly
        server-side while the endpoint still returns 200 (the token save is
        independent and must not regress because of this). Once the outbox
        is restored, an identical retry must succeed and produce exactly one
        outbox file -- proving the failed first attempt never claimed the
        de-dupe slot.
        """
        chat_id = "e2e_live_forced_failure_1"
        body = {
            "chat_id": chat_id,
            "access_token": "ya29.e2e-fake",
            "refresh_token": "1//e2e-fake",
            "expires_at": _future_iso(),
            "scope": "https://www.googleapis.com/auth/calendar.events",
        }
        outbox_dir = live_server.dirs["outbox"]

        # Block the real outbox directory the live subprocess writes to.
        assert outbox_dir.is_dir()
        shutil.rmtree(outbox_dir)
        outbox_dir.write_text("blocking file, not a directory -- forces mkdir() to fail")

        try:
            failed_resp = live_server.post(
                "/api/push-calendar-token", json_body=body, secret="e2e-test-internal-secret"
            )
            # The endpoint must still report success to the caller: the
            # OAuth token itself was saved; only the best-effort user-facing
            # confirmation failed, and that must never surface as a 5xx to
            # the pusher (myownlobster.ai) or block the token save.
            assert failed_resp.status_code == 200, live_server.server_log()

            token_path = live_server.dirs["gcal_tokens"] / f"{chat_id}.json"
            assert token_path.exists(), "token save must succeed independently of the confirmation path"

            # No confirmation could have been written -- outbox_dir is a file.
            assert not list(outbox_dir.glob(f"*{chat_id}*.json"))
        finally:
            # Restore the real outbox directory.
            outbox_dir.unlink()
            outbox_dir.mkdir(parents=True)

        # Retry: must succeed and must NOT be treated as a duplicate, proving
        # the failed attempt above never claimed the (scope, chat_id) slot.
        retry_resp = live_server.post(
            "/api/push-calendar-token", json_body=body, secret="e2e-test-internal-secret"
        )
        assert retry_resp.status_code == 200, live_server.server_log()

        outbox_files = list(outbox_dir.glob(f"*{chat_id}*.json"))
        assert len(outbox_files) == 1, (
            f"retry after a failed delivery must succeed (not be deduped away); "
            f"found {outbox_files}. Log:\n{live_server.server_log()}"
        )
        reply = json.loads(outbox_files[0].read_text())
        _assert_matches_lobster_bot_outbox_schema(reply, expected_chat_id=chat_id)
