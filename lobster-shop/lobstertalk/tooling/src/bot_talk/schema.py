"""
Bot-talk message schema — Signal Theory structured envelope.

Implements the S = (M, G, T, F, W) framework described in
sayhar/project-lobstertalk#21.

All bot-talk messages should use BotTalkMessage to build their payload.
The schema is backwards-compatible: legacy freeform messages (with only
sender/content fields) are still accepted by the server; the new fields
are additive.

Quick usage
-----------
from bot_talk.schema import BotTalkMessage, SpeechAct, Genre

msg = BotTalkMessage.heartbeat(
    sender="MyLobster",  # replace with your MY_LOBSTER_NAME
    body="All systems nominal",
    next_expected_at="2026-03-26T03:00:00Z",
)
payload = msg.to_dict()   # → dict suitable for HTTP POST

# On the receiver side:
msg = BotTalkMessage.from_dict(raw_payload)
if msg.speech_act == SpeechAct.QUERY:
    # must respond
    ...
if msg.ack_required:
    # post an ACK reply
    ...
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations (T dimension — type/variety of the signal)
# ---------------------------------------------------------------------------

class SpeechAct(str, enum.Enum):
    """The illocutionary type of the message (Variety dimension T).

    Allows the receiver to route deterministically — no NLP guessing needed.

    Values
    ------
    HEARTBEAT   Periodic liveness signal.  No response needed.
    INFORM      One-way information share.  No response needed.
    QUERY       A question that requires a response.
    COMMIT      A promise / obligation has been made.
    DECIDE      An outcome has been settled — conveys a decision.
    ALERT       Requires immediate routing to a human operator.
    ACK         Receipt confirmation.  No further response needed.
    """
    HEARTBEAT = "heartbeat"
    INFORM    = "inform"
    QUERY     = "query"
    COMMIT    = "commit"
    DECIDE    = "decide"
    ALERT     = "alert"
    ACK       = "ack"


class Genre(str, enum.Enum):
    """The recognised message form (G dimension).

    Wire format uses kebab-case (e.g. "status-update").
    Python enum members use SCREAMING_SNAKE_CASE; .value gives the wire string.

    Signal Theory mapping (Luna, "Signal Theory: The Architecture of Optimal Intent Encoding
    in Communication Systems", MIOSA Research, Feb 2026, DOI: 10.5281/zenodo.18774174):
      StatusUpdate   — Informative act, routine state broadcast
      TaskUpdate     — Directive act, work item state change
      Query          — Question act, request for information
      Proposal       — Commissive act, offer/suggestion requiring response
      Decision       — Declaration act, explicit agreement capture
      Alert          — Algedonic channel, urgent bypass path (from Beer's VSM, cited by Luna)
      Heartbeat      — Phatic act, "I'm alive" health check
      Acknowledgment — Affirmative act, receipt confirmation
    """
    STATUS_UPDATE  = "status-update"
    TASK_UPDATE    = "task-update"
    QUERY          = "query"
    PROPOSAL       = "proposal"
    DECISION       = "decision"
    ALERT          = "alert"
    HEARTBEAT      = "heartbeat"
    ACKNOWLEDGMENT = "acknowledgment"


# ---------------------------------------------------------------------------
# Structured message envelope
# ---------------------------------------------------------------------------

class BotTalkMessage:
    """Structured bot-talk message (full S = M, G, T, F, W envelope).

    Attributes
    ----------
    sender         Who sent this message.
    ts             ISO-8601 timestamp (UTC).
    genre          Recognised message form (Genre enum).
    speech_act     Illocutionary type (SpeechAct enum).
    body           Free-form content dict (structure depends on genre).
    reply_to       ts of the message being responded to, or None.
    ack_required   Whether the sender expects an explicit ACK.
    message_id     Stable opaque ID for this message (used in ACKs).
    tier           Privacy tier for this message. Controls what receiving bots
                   may do with the content.

                   TIER-BOT  — Bot infrastructure only (pings, heartbeats,
                               task completion). Default for all bot-to-bot
                               traffic.
                   TIER-0    — Public info (non-personal, freely shareable:
                               public facts, general knowledge).
                   TIER-1    — Shared-biz context (work notes, project status,
                               task updates). Currently shared between trusted
                               Lobster instances.
                   TIER-2    — Private personal context (calendar, plans,
                               preferences). Share only between your own bot
                               instances; never relay to third parties.
                   TIER-3    — Sensitive (health, financial, personal
                               relationships). Highest bar — use sparingly
                               even between your own instances.
    """

    def __init__(
        self,
        *,
        sender: str,
        ts: str | None = None,
        genre: Genre | str,
        speech_act: SpeechAct | str,
        body: dict[str, Any] | str,
        reply_to: str | None = None,
        ack_required: bool = False,
        message_id: str | None = None,
        tier: str = "TIER-BOT",
    ) -> None:
        self.sender = sender
        self.ts = ts or datetime.now(timezone.utc).isoformat()
        self.genre = Genre(genre) if isinstance(genre, str) else genre
        self.speech_act = SpeechAct(speech_act) if isinstance(speech_act, str) else speech_act
        self.body = {"text": body} if isinstance(body, str) else body
        self.reply_to = reply_to
        self.ack_required = ack_required
        self.message_id = message_id or f"{sender}:{self.ts}"
        self.tier = tier

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a dict suitable for JSON serialisation and HTTP POST.

        The dict includes both the new structured fields and the legacy
        ``content`` field so old receivers can still render something.
        """
        legacy_content = self._build_legacy_content()
        d: dict[str, Any] = {
            # Legacy fields (backwards compat with old bot-talk server)
            "sender": self.sender,
            "tier": self.tier,
            "genre": self.genre.value,
            "content": legacy_content,
            # Structured fields (new)
            "speech_act": self.speech_act.value,
            "ts": self.ts,
            "message_id": self.message_id,
            "ack_required": self.ack_required,
            "body": self.body,
        }
        if self.reply_to is not None:
            d["reply_to"] = self.reply_to
        return d

    def _build_legacy_content(self) -> str:
        """Synthesise a human-readable ``content`` string for old receivers."""
        if isinstance(self.body, dict) and "text" in self.body:
            text = self.body["text"]
        else:
            import json
            text = json.dumps(self.body)
        return f"[{self.speech_act.value.upper()}] {text}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotTalkMessage":
        """Deserialise a message dict (from HTTP GET or JSON file).

        Handles both structured messages (with speech_act) and legacy
        freeform messages (genre/content only).
        """
        sender = data.get("sender", "unknown")
        ts = data.get("ts") or data.get("timestamp")
        genre_raw = data.get("genre", "status-update")
        tier = data.get("tier", "TIER-BOT")

        # Attempt to coerce genre — fall back to STATUS_UPDATE if unknown
        try:
            genre = Genre(genre_raw)
        except ValueError:
            genre = Genre.STATUS_UPDATE

        # speech_act — new field; infer from genre if absent (legacy compat)
        speech_act_raw = data.get("speech_act")
        if speech_act_raw:
            try:
                speech_act = SpeechAct(speech_act_raw)
            except ValueError:
                speech_act = SpeechAct.INFORM
        else:
            speech_act = _infer_speech_act(genre)

        body = data.get("body") or {"text": data.get("content", "")}
        reply_to = data.get("reply_to")
        ack_required = bool(data.get("ack_required", False))
        message_id = data.get("message_id")

        return cls(
            sender=sender,
            ts=ts,
            genre=genre,
            speech_act=speech_act,
            body=body,
            reply_to=reply_to,
            ack_required=ack_required,
            message_id=message_id,
            tier=tier,
        )

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def heartbeat(
        cls,
        sender: str,
        body: str | dict[str, Any],
        next_expected_at: str | None = None,
        **kwargs: Any,
    ) -> "BotTalkMessage":
        """Build a Heartbeat message (T=HEARTBEAT, ack_required=False)."""
        if isinstance(body, str):
            body_dict: dict[str, Any] = {"text": body}
        else:
            body_dict = dict(body)
        if next_expected_at:
            body_dict["next_expected_at"] = next_expected_at
        return cls(
            sender=sender,
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body=body_dict,
            ack_required=False,
            **kwargs,
        )

    @classmethod
    def query(
        cls,
        sender: str,
        body: str | dict[str, Any],
        ack_required: bool = True,
        **kwargs: Any,
    ) -> "BotTalkMessage":
        """Build a Query message (T=QUERY, ack_required=True by default)."""
        return cls(
            sender=sender,
            genre=Genre.QUERY,
            speech_act=SpeechAct.QUERY,
            body=body,
            ack_required=ack_required,
            **kwargs,
        )

    @classmethod
    def inform(
        cls,
        sender: str,
        body: str | dict[str, Any],
        genre: Genre = Genre.STATUS_UPDATE,
        **kwargs: Any,
    ) -> "BotTalkMessage":
        """Build an Inform message (T=INFORM, ack_required=False)."""
        return cls(
            sender=sender,
            genre=genre,
            speech_act=SpeechAct.INFORM,
            body=body,
            ack_required=False,
            **kwargs,
        )

    @classmethod
    def alert(
        cls,
        sender: str,
        body: str | dict[str, Any],
        ack_required: bool = True,
        **kwargs: Any,
    ) -> "BotTalkMessage":
        """Build an Alert message (T=ALERT, ack_required=True by default)."""
        return cls(
            sender=sender,
            genre=Genre.ALERT,
            speech_act=SpeechAct.ALERT,
            body=body,
            ack_required=ack_required,
            **kwargs,
        )

    @classmethod
    def ack(
        cls,
        sender: str,
        reply_to_id: str,
        reply_to_ts: str,
        body: str = "acknowledged",
        **kwargs: Any,
    ) -> "BotTalkMessage":
        """Build an explicit ACK message responding to a query or alert.

        Args:
            sender:       The acknowledging bot's name.
            reply_to_id:  message_id of the message being acknowledged.
            reply_to_ts:  ts of the message being acknowledged (used as
                          reply_to for timeline threading).
            body:         Optional acknowledgement note.
        """
        return cls(
            sender=sender,
            genre=Genre.ACKNOWLEDGMENT,
            speech_act=SpeechAct.ACK,
            body={"text": body, "ack_for": reply_to_id},
            reply_to=reply_to_ts,
            ack_required=False,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def needs_ack(self) -> bool:
        """Return True if this message expects an explicit acknowledgement."""
        return self.ack_required

    def is_routing_to_human(self) -> bool:
        """Return True if this message should be escalated to a human operator."""
        return self.speech_act == SpeechAct.ALERT

    def requires_response(self) -> bool:
        """Return True if this message requires any response (ACK or answer)."""
        return self.speech_act in (SpeechAct.QUERY, SpeechAct.ALERT) or self.ack_required

    def __repr__(self) -> str:
        return (
            f"BotTalkMessage(sender={self.sender!r}, "
            f"speech_act={self.speech_act.value!r}, "
            f"genre={self.genre.value!r}, "
            f"ts={self.ts!r})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _infer_speech_act(genre: Genre) -> SpeechAct:
    """Infer a speech act from genre for legacy messages that lack speech_act."""
    _GENRE_TO_SPEECH_ACT = {
        Genre.HEARTBEAT:      SpeechAct.HEARTBEAT,
        Genre.TASK_UPDATE:    SpeechAct.INFORM,
        Genre.QUERY:          SpeechAct.QUERY,
        Genre.PROPOSAL:       SpeechAct.COMMIT,
        Genre.DECISION:       SpeechAct.DECIDE,
        Genre.ALERT:          SpeechAct.ALERT,
        Genre.STATUS_UPDATE:  SpeechAct.INFORM,
        Genre.ACKNOWLEDGMENT: SpeechAct.ACK,
    }
    return _GENRE_TO_SPEECH_ACT.get(genre, SpeechAct.INFORM)
