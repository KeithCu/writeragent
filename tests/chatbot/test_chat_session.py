"""Pure unit tests for ChatSession message/history rules (no UNO)."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.chat_session import (
    ChatSession,
    SNAPSHOT_HEADER,
    snapshot_text,
    strip_legacy_document_suffix,
)
from plugin.framework.errors import WriterAgentException


def test_init_with_system_prompt_sets_system_message():
    session = ChatSession(system_prompt="You are helpful.")
    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "system", "content": "You are helpful."}
    assert session.base_system_prompt == "You are helpful."
    assert session.document_context == ""
    assert session.compaction is None


def test_init_without_system_prompt_leaves_messages_empty():
    session = ChatSession()
    assert session.messages == []
    assert session.base_system_prompt == ""


def test_set_system_context_inserts_and_replaces():
    session = ChatSession()
    session.set_system_context("Base", "Doc body")
    assert session.messages[0]["role"] == "system"
    assert session.messages[0]["content"] == "Base"
    assert session.document_context == "Doc body"

    session.add_user_message("hi")
    session.set_system_context("Base2", "Other")
    assert session.messages[0]["content"] == "Base2"
    assert session.document_context == "Other"
    assert session.messages[1]["role"] == "user"


def test_set_system_context_clears_document_block():
    session = ChatSession(system_prompt="Base")
    session.set_system_context("Base", "Doc body")
    assert session.messages[0]["content"] == "Base"
    assert session.document_context == "Doc body"

    session.set_system_context("Base", "")
    assert session.messages[0]["content"] == "Base"
    assert session.document_context == ""


def test_add_assistant_message_replaces_open_transcript_row():
    """The streamed paint row is not a second copy of the committed answer."""
    session = ChatSession(system_prompt="Sys")
    session.messages.append({"role": "assistant", "content": "stream", "_open_transcript": True})
    session.add_assistant_message(content="<p>final</p>")
    assistant = [message for message in session.messages if message["role"] == "assistant"]
    assert assistant == [{"role": "assistant", "content": "<p>final</p>"}]


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
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-1")

    mock_db.replace_messages.assert_called_once_with([{"role": "system", "content": "Sys"}])
    mock_db.reset_mock()

    session.add_user_message("u")
    session.add_assistant_message(content="a", tool_calls=[{"id": "t1"}])
    session.add_assistant_message(tool_calls=[{"id": "t2"}])
    session.add_tool_result("t1", "tool-out")

    assert mock_db.add_message.call_args_list == [
        (("user", "u"),),
        (("assistant", "a"),),
    ]
    # Tool results stay in-memory only.
    assert not any(c.args and c.args[0] == "tool" for c in mock_db.add_message.call_args_list)


def test_load_failure_does_not_seed_system_row():
    import json

    mock_db = MagicMock()
    mock_db.get_messages.side_effect = json.JSONDecodeError("bad", "doc", 0)
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-bad")

    assert session.messages == []
    mock_db.add_message.assert_not_called()


def test_load_oserror_does_not_seed_system_row():
    mock_db = MagicMock()
    mock_db.get_messages.side_effect = OSError("disk")
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-os")

    assert session.messages == []
    mock_db.add_message.assert_not_called()


def test_clear_resets_messages_and_document_context():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = []
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Sys", session_id="sid-clear")

    session.set_system_context("Sys", "Doc")
    session.add_user_message("keep me not")
    mock_db.reset_mock()

    session.compaction = object()
    session.active_specialized_domain = "tables"
    session.python_tool_domain = "numpy"

    session.clear()

    assert session.document_context == ""
    assert session.compaction is None
    assert session.active_specialized_domain is None
    assert session.python_tool_domain is None

    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "system", "content": "Sys"}
    mock_db.replace_messages.assert_called_once_with([{"role": "system", "content": "Sys"}])


def test_clear_resets_compaction():
    session = ChatSession(system_prompt="Sys")
    session.compaction = object()
    session.clear()
    assert session.compaction is None


def test_history_load_uses_persisted_messages():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = [
        {"role": "system", "content": "Loaded sys"},
        {"role": "user", "content": "prior"},
    ]
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
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
    assert session.messages[0]["content"] == "FRESH PROMPT"
    assert session.messages[1]["role"] == "user"


def test_history_load_failure_still_applies_system_prompt():
    with patch(
        "plugin.chatbot.chat_session.get_chat_history",
        side_effect=WriterAgentException("db down"),
    ):
        session = ChatSession(system_prompt="Fallback sys", session_id="sid-fail")

    assert session.db is None
    assert session.messages == [{"role": "system", "content": "Fallback sys"}]


def test_set_system_context_missing_role_inserts_system():
    session = ChatSession()
    session.messages = [{"content": "orphan"}]
    session.set_system_context("Base", "Doc")
    assert session.messages[0]["role"] == "system"
    assert session.messages[0]["content"] == "Base"
    assert session.document_context == "Doc"
    assert session.messages[1] == {"content": "orphan"}

    session.set_system_context("Base", "Doc2")
    assert session.messages[0]["role"] == "system"
    assert session.messages[0]["content"] == "Base"
    assert session.document_context == "Doc2"
    assert session.messages[1] == {"content": "orphan"}


def test_set_system_context_nondict_first_row_inserts_system():
    session = ChatSession()
    session.messages = ["nope"]
    session.set_system_context("Base", "")
    assert session.messages[0] == {"role": "system", "content": "Base"}
    assert session.messages[1] == "nope"


def test_logger_is_module_scoped():
    from plugin.chatbot import chat_session

    assert chat_session.log.name == "plugin.chatbot.chat_session"


def test_messages_for_request_user_send_stable_prefix():
    session = ChatSession(system_prompt="Base system")
    session.add_user_message("Hello AI")
    session.document_context = "Doc state 1"
    req1 = session.messages_for_request()

    session.document_context = "Doc state 2 (selected text changed)"
    req2 = session.messages_for_request()

    assert len(req1) == 2
    assert len(req2) == 2
    assert req1[0] == req2[0]  # Base system unchanged
    assert req1[1]["role"] == "user"
    assert req2[1]["role"] == "user"
    assert req1[1]["content"][:-1] == req2[1]["content"][:-1]
    assert req1[1]["content"][-1]["text"] == snapshot_text("Doc state 1")
    assert req2[1]["content"][-1]["text"] == snapshot_text("Doc state 2 (selected text changed)")

    # Stored user message content is still the original string
    assert session.messages[1]["content"] == "Hello AI"


def test_messages_for_request_tool_loop_stable_prefix():
    mock_db = MagicMock()
    mock_db.get_messages.return_value = []
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="Base system", session_id="sid-prefix")
    mock_db.reset_mock()

    session.add_user_message("Do math")
    session.document_context = "Doc 1"
    assert session.messages_for_request()

    # Append assistant tool call and tool result
    session.add_assistant_message(tool_calls=[{"id": "c1", "function": {"name": "calc"}}])
    session.add_tool_result("c1", "42")
    session.document_context = "Doc 2"
    req_tool1 = session.messages_for_request()

    # The new payload's last message is {"role": "user", "content": snapshot}
    assert req_tool1[-1] == {"role": "user", "content": snapshot_text("Doc 2")}
    # The messages before it equal the stored history
    assert req_tool1[:-1] == session.messages
    # The stored user content is still the original string, not the first snapshot
    assert session.messages[1]["content"] == "Do math"

    # A further tool row plus another excerpt:
    session.add_assistant_message(tool_calls=[{"id": "c2", "function": {"name": "calc2"}}])
    session.add_tool_result("c2", "84")
    session.document_context = "Doc 3"
    req_tool2 = session.messages_for_request()

    # The payload with the snapshot message removed extends the previous payload with snapshot removed
    assert req_tool2[:-1] == req_tool1[:-1] + session.messages[len(req_tool1) - 1 :]
    # Shared prefix is byte-identical (req_tool1[:-1] is a prefix of req_tool2[:-1])
    assert req_tool2[: len(req_tool1) - 1] == req_tool1[:-1]

    # After those calls, session.messages has no SNAPSHOT_HEADER
    for m in session.messages:
        content_str = str(m.get("content") or "")
        assert SNAPSHOT_HEADER not in content_str

    # A mock DB's add_message / replace_messages were not called with that header
    for call in mock_db.add_message.call_args_list:
        for arg in call.args:
            assert SNAPSHOT_HEADER not in str(arg)
    for call in mock_db.replace_messages.call_args_list:
        for arg in call.args:
            assert SNAPSHOT_HEADER not in str(arg)


def test_history_load_strips_legacy_document_suffix_with_inner_end_doc():
    legacy_content = (
        "Base\n\n[DOCUMENT CONTENT]\n"
        "[DOCUMENT START]\nhello\n[END DOCUMENT]\nSECRET\n[END DOCUMENT]"
    )
    mock_db = MagicMock()
    mock_db.get_messages.return_value = [
        {"role": "system", "content": legacy_content},
        {"role": "user", "content": "Question"},
    ]
    with patch("plugin.chatbot.chat_session.get_chat_history", return_value=mock_db):
        session = ChatSession(system_prompt="New base", session_id="sid-legacy")

    assert session.messages[0] == {"role": "system", "content": "Base"}
    assert session.messages[1] == {"role": "user", "content": "Question"}
    mock_db.replace_messages.assert_not_called()
    mock_db.add_message.assert_not_called()

    # Also prove strip_legacy_document_suffix on new header does not match
    new_header_content = f"Base\n\n{SNAPSHOT_HEADER}\nSECRET\n[END DOCUMENT]"
    assert strip_legacy_document_suffix(new_header_content) == new_header_content

