"""Pure unit tests for ChatSession message/history rules (no UNO)."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.panel import ChatSession
from plugin.framework.errors import WriterAgentException


def test_init_with_system_prompt_sets_system_message():
    session = ChatSession(system_prompt="You are helpful.")
    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "system", "content": "You are helpful."}
    assert session.base_system_prompt == "You are helpful."
    assert session.document_context == ""


def test_init_without_system_prompt_leaves_messages_empty():
    session = ChatSession()
    assert session.messages == []
    assert session.base_system_prompt == ""


def test_set_system_context_inserts_and_replaces():
    session = ChatSession()
    session.set_system_context("Base", "Doc body")
    assert session.messages[0]["role"] == "system"
    assert "[DOCUMENT CONTENT]\nDoc body\n[END DOCUMENT]" in session.messages[0]["content"]
    assert session.document_context == "Doc body"

    session.add_user_message("hi")
    session.set_system_context("Base2", "Other")
    assert session.messages[0]["content"].startswith("Base2")
    assert "Other" in session.messages[0]["content"]
    assert session.messages[1]["role"] == "user"


def test_set_system_context_clears_document_block():
    session = ChatSession(system_prompt="Base")
    session.set_system_context("Base", "Doc body")
    assert "[DOCUMENT CONTENT]" in session.messages[0]["content"]

    session.set_system_context("Base", "")
    assert session.messages[0]["content"] == "Base"
    assert "[DOCUMENT CONTENT]" not in session.messages[0]["content"]
    assert session.document_context == ""


def test_add_message_shapes():
    session = ChatSession(system_prompt="Sys")
    session.add_user_message("hello")
    session.add_assistant_message(
        content="world",
        tool_calls=[{"id": "t1", "function": {"name": "ping"}}],
        reasoning_replay={"reasoning_content": "think"},
    )
    session.add_assistant_message()
    session.add_tool_result("t1", '{"ok": true}')

    assert session.messages[1] == {"role": "user", "content": "hello"}
    assistant = session.messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "world"
    assert assistant["tool_calls"] == [{"id": "t1", "function": {"name": "ping"}}]
    assert assistant["reasoning_content"] == "think"
    assert session.messages[3] == {"role": "assistant", "content": ""}
    assert session.messages[4] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": '{"ok": true}',
    }


def test_persist_rules_with_mock_db():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = []
    with patch("plugin.chatbot.panel.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-1")

    mock_db.add_message.assert_called_with("system", "Sys")
    mock_db.reset_mock()

    session.add_user_message("u")
    session.add_assistant_message(content="a", tool_calls=[{"id": "t1"}])
    session.add_assistant_message(tool_calls=[{"id": "t2"}])
    session.add_tool_result("t1", "tool-out")

    assert mock_db.add_message.call_args_list == [
        (("user", "u"),),
        (("assistant", "a"),),
        (("assistant", None),),
    ]
    # Tool results stay in-memory only.
    assert not any(c.args and c.args[0] == "tool" for c in mock_db.add_message.call_args_list)


def test_clear_resets_messages_and_document_context():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = []
    with patch("plugin.chatbot.panel.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-clear")

    session.set_system_context("Sys", "Doc")
    session.add_user_message("keep me not")
    mock_db.reset_mock()

    session.clear()

    mock_db.clear.assert_called_once()
    assert session.document_context == ""
    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "system", "content": "Sys"}
    mock_db.add_message.assert_called_with("system", "Sys")


def test_history_load_uses_persisted_messages():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = [
        {"role": "system", "content": "Loaded sys"},
        {"role": "user", "content": "prior"},
    ]
    with patch("plugin.chatbot.panel.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Ignored when history present", session_id="sid-load")

    assert session.db is mock_db
    assert session.messages == [
        {"role": "system", "content": "Loaded sys"},
        {"role": "user", "content": "prior"},
    ]
    mock_db.add_message.assert_not_called()


def test_refresh_document_context_sets_prompt_and_excerpt():
    session = ChatSession(system_prompt="stale")
    session.add_user_message("prior")
    model = MagicMock()
    ctx = MagicMock()

    with (
        patch("plugin.doc.document_helpers.get_document_context_for_chat", return_value="DOC BODY") as mock_doc,
        patch("plugin.framework.prompts.get_chat_system_prompt_for_document", return_value="FRESH PROMPT") as mock_prompt,
        patch("plugin.framework.config.get_config", return_value="extra notes"),
    ):
        session.refresh_document_context(model, ctx)

    mock_doc.assert_called_once()
    assert mock_doc.call_args.args[0] is model
    assert mock_doc.call_args.kwargs["include_end"] is True
    assert mock_doc.call_args.kwargs["include_selection"] is True
    assert mock_doc.call_args.kwargs["ctx"] is ctx
    mock_prompt.assert_called_once_with(model, "extra notes", ctx=ctx)
    assert session.base_system_prompt == "FRESH PROMPT"
    assert session.document_context == "DOC BODY"
    assert session.messages[0]["content"].startswith("FRESH PROMPT")
    assert "[DOCUMENT CONTENT]\nDOC BODY\n[END DOCUMENT]" in session.messages[0]["content"]
    assert session.messages[1]["role"] == "user"


def test_history_load_failure_still_applies_system_prompt():
    with patch(
        "plugin.chatbot.panel.get_chat_history",
        side_effect=WriterAgentException("db down"),
    ):
        session = ChatSession(system_prompt="Fallback sys", session_id="sid-fail")

    assert session.db is None
    assert session.messages == [{"role": "system", "content": "Fallback sys"}]
