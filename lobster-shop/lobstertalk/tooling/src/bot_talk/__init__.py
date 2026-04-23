"""
bot_talk — AlbertLobster bot-to-bot communication toolkit.

Public API
----------
from bot_talk.schema import BotTalkMessage, SpeechAct, Genre
from bot_talk.mirror import mirror_outbound, mirror_inbound
"""

from bot_talk.schema import BotTalkMessage, Genre, SpeechAct

__all__ = ["BotTalkMessage", "Genre", "SpeechAct"]
