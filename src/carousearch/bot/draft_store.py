"""Where each chat's active ChatDraft lives. In memory for now; the same get/set/clear can sit on SQLite later."""

from carousearch.bot.state import ChatDraft


class DraftStore:
    def __init__(self) -> None:
        self._drafts_by_chat_id: dict[int, ChatDraft] = {}

    def get(self, chat_id: int) -> ChatDraft | None:
        return self._drafts_by_chat_id.get(chat_id)

    def set(self, chat_id: int, draft: ChatDraft) -> None:
        self._drafts_by_chat_id[chat_id] = draft

    def clear(self, chat_id: int) -> None:
        self._drafts_by_chat_id.pop(chat_id, None)
