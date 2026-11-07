from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.history_db import get_chat_history

log = logging.getLogger(__name__)

from plugin.framework.client.shim_helpers import snapshot_text


def strip_legacy_document_suffix(content: str) -> str:
    """If content endswith '\n[END DOCUMENT]' and contains '\n\n[DOCUMENT CONTENT]\n',
    return content[:content.rfind(that marker)]. Otherwise return content.
    """
    marker = "\n\n[DOCUMENT CONTENT]\n"
    if content.endswith("\n[END DOCUMENT]") and marker in content:
        idx = content.rfind(marker)
        if idx != -1:
            return content[:idx]
    return content


def history_open_failed_text() -> str:
    """Return _("Chat history could not be opened. This conversation will not be saved.")."""
    from plugin.framework.i18n import _

    return _("Chat history could not be opened. This conversation will not be saved.")


def request_tail(session: Any) -> str:
    """Return dynamic per-request context (such as the document snapshot).

    Everything that changes per request lives after the cache breakpoint so
    earlier history and system prompts stay cacheable across turns.
    """
    doc_text = getattr(session, "document_context", "") or ""
    if doc_text:
        snap = snapshot_text(doc_text)
        if snap:
            return snap
    return ""

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
    history_notice: str

    def __init__(self, system_prompt: str | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.db = None
        self.history_notice = ""
        self.messages = []
        self.base_system_prompt = system_prompt or ""
        self.document_context = ""

        self.active_specialized_domain = None
        self.python_tool_domain = None
        # Cached compact view (CompactionState). Duck-typed by compaction.py;
        # never persisted. New chat / clear() must drop it or the next send
        # would keep summarizing against a stale first_kept_index.
        self.compaction = None

        if session_id:
            try:
                self.db = get_chat_history(session_id)
                self.messages = self.db.get_messages()
                if self.messages:
                    first = self.messages[0]
                    if isinstance(first, dict) and first.get("role") == "system" and isinstance(first.get("content"), str):
                        first["content"] = strip_legacy_document_suffix(first["content"])
            except Exception:
                # Load failure with an open DB must not seed a lone system row over corrupt history.
                log.exception("ChatSession history load failed")
                if self.db is None:
                    self.history_notice = history_open_failed_text()
                else:
                    # Backend opened, then get_messages raised: must not insert
                    # a system row, which would replace a corrupt history with a
                    # one-message session.
                    return

        # Empty new session still seeds and is written. If the backend never
        # opened, there is no file to protect, so the prompt stays in memory
        # for this run and nothing is written.
        if not self.messages and self.base_system_prompt:
            self._seed_system_message()

    def _seed_system_message(self) -> None:
        """Reset memory to just the system prompt and write history once."""
        self.messages = []
        self.document_context = ""
        if self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
        if self.db:
            self.db.replace_messages(list(self.messages))

    def set_system_context(self, base_prompt: str, doc_text: str = "") -> None:
        """Update the system prompt and document context.

        base_system_prompt = base_prompt; document_context = doc_text.
        messages[0] content is base_prompt only. Insert a system row when the
        first row is not a system dict. Do not touch the DB.
        """
        self.base_system_prompt = base_prompt
        self.document_context = doc_text

        # Rows without role=system get a system header inserted; malformed dicts must not raise KeyError.
        first = self.messages[0] if self.messages else None
        if not isinstance(first, dict) or first.get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": base_prompt})
        else:
            first["content"] = base_prompt

    def refresh_document_context(self, model: Any, ctx: Any) -> None:
        """Reload the Chat system prompt and document context from the live document.

        Excerpt is stored on document_context, not messages[0].

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

    def messages_for_request(self) -> list[dict[str, Any]]:
        """Return the messages to send to the LLM for a request.

        Starts with messages_for_llm(self), then folds request_tail(self)
        into the trailing message:
        - On normal chat turns (ending in a user message), the snapshot is
          placed at the tail of the newest user turn as a content block, keeping
          the newest user turn a consistent shape (list of content blocks) and
          preserving prompt cache prefix stability.
        - On tool rounds (ending in a tool message), the snapshot is folded
          into the last tool result message so models (like gpt-oss-20b) do not
          see an instruction-less trailing user message and enter runaway edit loops.
        Does not mutate self.messages or self.compaction.
        """
        from plugin.chatbot.compaction import messages_for_llm

        view = [dict(m) for m in messages_for_llm(self)]
        if not view:
            return view

        tail = request_tail(self)
        last = view[-1]
        role = last.get("role") if isinstance(last, dict) else None

        if role == "user":
            # Strict-alternation templates reject two adjacent user turns; the tail folds into the last user message.
            content = last.get("content")
            if isinstance(content, str):
                parts = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                parts = list(content)
            else:
                parts = [{"type": "text", "text": str(content or "")}]
            if tail:
                parts.append({"type": "text", "text": tail})
            new_last = dict(last, content=parts)
            return view[:-1] + [new_last]

        if role == "tool" and tail:
            # A user turn after a tool result reads as a new request (gpt-oss-20b re-edited in a loop); folding the snapshot into the tool result keeps the model in tool-response state.
            content = last.get("content")
            new_content: Any
            if isinstance(content, str):
                new_content = f"{content}\n\n{tail}" if content else tail
            elif isinstance(content, list):
                new_content = list(content) + [{"type": "text", "text": tail}]
            else:
                new_content = f"{content}\n\n{tail}" if content is not None else tail
            new_last = dict(last, content=new_content)
            return view[:-1] + [new_last]

        return view

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})
        if self.db:
            self.db.add_message("user", content)

    def add_assistant_message(self, content: Any = None, tool_calls: Any = None, reasoning_replay: Any = None) -> None:
        msg = {"role": "assistant", "content": content or ""}
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
        # Tool-only turns keep content out of disk history; empty assistant text is not persisted.
        if self.db and content:
            self.db.add_message("assistant", content)

    def add_tool_result(self, tool_call_id: str, content: Any) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
        # Note: We do NOT persist tool results to history_db.
        # This keeps the persistent history clean of tool formatting requirements.

    def clear(self) -> None:
        """Reset to just the system prompt."""
        self.compaction = None
        # The next send advertises tools from these fields. Leaving the
        # previous delegate set meant Clear still offered that domain.
        self.active_specialized_domain = None
        self.python_tool_domain = None
        self._seed_system_message()


def message_text(content: Any, *, strip_html: bool = False) -> str:
    """Normalize user/assistant message content (string or multimodal parts list) to plain text."""
    if not isinstance(content, (str, list)):
        return ""
    if isinstance(content, str):
        if strip_html:
            from plugin.framework.html_stripper import strip_html_tags

            return strip_html_tags(content)
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            if part:
                parts.append(part)
        elif isinstance(part, dict):
            if part.get("type") in ("text", None):
                txt = part.get("text")
                if txt is not None:
                    s = str(txt)
                    if s:
                        parts.append(s)
    text = "\n".join(parts)
    if strip_html:
        from plugin.framework.html_stripper import strip_html_tags

        return strip_html_tags(text)
    return text

