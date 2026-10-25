"""Where each chat's active SearchSession lives. In memory: a restart forgets every session.

The same get/set/clear could sit on SQLite later without changing the handlers.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from carousearch.bot.state import SESSION_IDLE_TIMEOUT, SearchSession, is_expired


class SessionStore:
    """At most one session per chat. A session idle for longer than `idle_timeout` no longer counts."""

    def __init__(self, clock: Callable[[], datetime], idle_timeout: timedelta = SESSION_IDLE_TIMEOUT) -> None:
        self._clock = clock
        self._idle_timeout = idle_timeout
        self._sessions_by_chat_id: dict[int, SearchSession] = {}

    def get(self, chat_id: int) -> SearchSession | None:
        """The chat's active session; None when there is none or it has expired."""
        session = self._sessions_by_chat_id.get(chat_id)
        if session is None or self._has_expired(session):
            return None
        return session

    def remove_if_expired(self, chat_id: int) -> bool:
        """Forget the chat's session if it has expired. Returns True if one was removed."""
        session = self._sessions_by_chat_id.get(chat_id)
        if session is None or not self._has_expired(session):
            return False
        del self._sessions_by_chat_id[chat_id]
        return True

    def set(self, chat_id: int, session: SearchSession) -> None:
        self._sessions_by_chat_id[chat_id] = session

    def clear(self, chat_id: int) -> None:
        self._sessions_by_chat_id.pop(chat_id, None)

    def _has_expired(self, session: SearchSession) -> bool:
        return is_expired(session, self._clock(), self._idle_timeout)
