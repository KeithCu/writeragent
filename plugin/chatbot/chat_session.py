from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.history_db import get_chat_history

log = logging.getLogger("plugin.chatbot.panel")


# ---------------------------------------------------------------------------
# ChatSession - holds conversation history for multi-turn chat
# ---------------------------------------------------------------------------


class ChatSession:
    """Maintains the message history for one sidebar chat session."""

    session_id: str | None
    db: Any
    messages: list[dict[str, Any]]
    base_system_prompt: str
    document_context: str
    active_specialized_domain: str | None
    python_tool_domain: str | None
    compaction: Any

    def __init__(self, system_prompt: str | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.db = None
        self.messages = []
        self.base_system_prompt = system_prompt or ""
        self.document_context = ""

        self.active_specialized_domain = None
        self.python_tool_domain = None
        # Cached compact view (CompactionState). Duck-typed by compaction.py;
        # never persisted. New chat / clear() must drop it or the next send
        # would keep summarizing against a stale first_kept_index.
        self.compaction = None

        history_load_failed = False
        if session_id:
            try:
                self.db = get_chat_history(session_id)
                self.messages = self.db.get_messages()
            except Exception:
                # JSONDecodeError and sqlite json.loads used to land here with
                # messages still []. The seed below then add_message'd a system
                # row without deleting the old ones (or over a truncated file).
                log.exception("ChatSession history load failed")
                history_load_failed = True

        # Empty new session still seeds and is written. A failed read (the
        # backend opened, then get_messages raised) must not insert a system
        # row: that replaced a corrupt history with a one-message session.
        # If the backend never opened, there is no file to protect, so the
        # prompt stays in memory for this run and nothing is written.
        if history_load_failed:
            if self.db is None and self.base_system_prompt:
                self.set_system_context(self.base_system_prompt, "")
        elif not self.messages and self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])

    def set_system_context(self, base_prompt: str, doc_text: str = "") -> None:
        """Update the system prompt and document context, combining them into the first message."""
        self.base_system_prompt = base_prompt
        self.document_context = doc_text
        
        content = base_prompt
        if doc_text:
            content += f"\n\n[DOCUMENT CONTENT]\n{doc_text}\n[END DOCUMENT]"
            
        # Bugfix: a loaded dict with no "role" raised KeyError on every send.
        # SQLite3History.get_messages keeps every JSON object, and refresh_document_context
        # calls set_system_context on every send. Anything that is not a system dict gets
        # a system row inserted in front, leaving the existing first row in place.
        first = self.messages[0] if self.messages else None
        if not isinstance(first, dict) or first.get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": content})
        else:
            first["content"] = content

    def refresh_document_context(self, model: Any, ctx: Any) -> None:
        """Reload the Chat system prompt and ``[DOCUMENT CONTENT]`` from the live document.

        Why this lives on ChatSession, not panel_factory: the factory only wires
        XDL/controls. Mode switch, each send, and mid-loop refresh after a
        mutating tool all need a fresh snapshot. Send/tool_loop call this
        helper; they do not import the builder.
        """
        from plugin.doc.document_helpers import get_document_context_for_chat
        from plugin.framework.config import get_config
        from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
        from plugin.framework.prompts import get_chat_system_prompt_for_document

        extra_instructions = str(get_config("additional_instructions") or "")
        base_prompt = get_chat_system_prompt_for_document(model, extra_instructions, ctx=ctx)
        doc_text = get_document_context_for_chat(
            model, CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True, ctx=ctx
        )
        self.set_system_context(base_prompt, doc_text)

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})
        if self.db:
            self.db.add_message("user", content)

    def add_assistant_message(self, content: Any = None, tool_calls: Any = None, reasoning_replay: Any = None) -> None:
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        else:
            msg["content"] = ""
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_replay:
            msg.update(reasoning_replay)
        # Streamed tokens sit in an open row so the sidebar can paint them.
        # This committed message replaces that row. Leaving both showed the
        # answer twice, and the open row is not a history write.
        if self.messages and self.messages[-1].get("_open_transcript"):
            self.messages.pop()
        self.messages.append(msg)
        # Tool calls stay out of history. content=None used to be written
        # anyway, and message_to_dict stored JSON null for a tool-only turn.
        # Memory keeps "" above; disk skips empty assistant text.
        if self.db and content:
            self.db.add_message("assistant", content)

    def add_tool_result(self, tool_call_id: str, content: Any) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
        # Note: We do NOT persist tool results to history_db.
        # This keeps the persistent history clean of tool formatting requirements.

    def clear(self) -> None:
        """Reset to just the system prompt."""
        self.messages = []
        self.document_context = ""
        self.compaction = None
        # The next send advertises tools from these fields. Leaving the
        # previous delegate set meant Clear still offered that domain.
        self.active_specialized_domain = None
        self.python_tool_domain = None
        if self.db:
            self.db.clear()
            
        if self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])
