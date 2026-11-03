"""Pure unit tests for ChatSession message/history rules (no UNO)."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.chat_session import (
    ChatSession,
    strip_legacy_document_suffix,
)
from plugin.framework.client.shim_helpers import (
    SNAPSHOT_HEADER,
    snapshot_text,
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

    session.add_assistant_message("Hello! How can I help you?")
    session.add_user_message("Second user prompt")
    session.document_context = "Doc state 2 (selected text changed)"
    req2 = session.messages_for_request()

    assert req1[0] == req2[0]  # Base system unchanged
    assert req2[1] == session.messages[1]  # Earlier user message in req2 is the committed history turn
    assert req2[1]["content"] == "Hello AI"
    # Trailing user message in req1 has tail folded in
    assert req1[-1]["role"] == "user"
    assert req1[-1]["content"][0]["text"] == "Hello AI"
    assert snapshot_text("Doc state 1") in req1[-1]["content"][1]["text"]
    # Trailing user message in req2 has second tail folded in
    assert req2[-1]["role"] == "user"
    assert req2[-1]["content"][0]["text"] == "Second user prompt"
    assert snapshot_text("Doc state 2 (selected text changed)") in req2[-1]["content"][1]["text"]

    # Stored user message content is still the original string
    assert session.messages[1]["content"] == "Hello AI"


def test_messages_for_request_tail_is_snapshot_only():
    """Date and memory live in the system prompt; request_tail is document snapshot only."""
    session = ChatSession(system_prompt="Base system")
    session.add_user_message("Hello")
    session.document_context = "Active document text"

    req = session.messages_for_request()
    # Trailing folded block contains only document snapshot
    content_blocks = req[-1]["content"]
    assert isinstance(content_blocks, list)
    assert len(content_blocks) == 2
    assert content_blocks[0] == {"type": "text", "text": "Hello"}
    assert snapshot_text("Active document text") == content_blocks[1]["text"]
    assert "Today's date is" not in content_blocks[1]["text"]
    assert "[USER PROFILE / MEMORY]" not in content_blocks[1]["text"]


def test_messages_for_request_never_has_adjacent_user_messages():
    """Strict alternating-role templates (Gemma, Mistral on LM Studio, llama.cpp, vLLM)
    fail if two user messages are adjacent. Folding guarantees no adjacent user messages."""
    def assert_no_adjacent_user(msgs):
        for idx in range(len(msgs) - 1):
            assert not (msgs[idx].get("role") == "user" and msgs[idx + 1].get("role") == "user"), (
                f"Adjacent user messages at index {idx} and {idx + 1}: {msgs[idx]} and {msgs[idx + 1]}"
            )

    # 1. Single user send with document context
    s1 = ChatSession(system_prompt="System")
    s1.add_user_message("Query 1")
    s1.document_context = "Doc A"
    req1 = s1.messages_for_request()
    assert_no_adjacent_user(req1)
    assert [m["role"] for m in req1] == ["system", "user"]

    # 2. Multi-turn conversation with document context
    s1.add_assistant_message("Answer 1")
    s1.add_user_message("Query 2")
    s1.document_context = "Doc B"
    req2 = s1.messages_for_request()
    assert_no_adjacent_user(req2)
    assert [m["role"] for m in req2] == ["system", "user", "assistant", "user"]

    # 3. Mid tool loop (user, assistant tool_calls, tool result) with document context
    s1.add_assistant_message(tool_calls=[{"id": "c1", "function": {"name": "lookup"}}])
    s1.add_tool_result("c1", "found result")
    s1.document_context = "Doc C"
    req3 = s1.messages_for_request()
    assert_no_adjacent_user(req3)
    assert [m["role"] for m in req3] == ["system", "user", "assistant", "user", "assistant", "tool"]

    # 4. Without document context
    s2 = ChatSession(system_prompt="System")
    s2.add_user_message("No doc query")
    s2.document_context = ""
    req4 = s2.messages_for_request()
    assert_no_adjacent_user(req4)
    assert [m["role"] for m in req4] == ["system", "user"]
    # Consistent shape: newest user turn is always a list of content blocks
    assert isinstance(req4[-1]["content"], list)
    assert req4[-1]["content"] == [{"type": "text", "text": "No doc query"}]


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

    # The payload's last message is the tool result with snapshot folded in
    assert req_tool1[-1]["role"] == "tool"
    assert req_tool1[-1]["tool_call_id"] == "c1"
    assert isinstance(req_tool1[-1]["content"], str)
    assert req_tool1[-1]["content"].startswith("42")
    assert snapshot_text("Doc 2") in req_tool1[-1]["content"]
    # The messages before it equal the stored history
    assert req_tool1[:-1] == session.messages[:-1]
    # The stored tool result content in session.messages is unchanged
    assert session.messages[-1]["content"] == "42"
    # The stored user content is still the original string, not the first snapshot
    assert session.messages[1]["content"] == "Do math"

    # A further tool row plus another excerpt:
    session.add_assistant_message(tool_calls=[{"id": "c2", "function": {"name": "calc2"}}])
    session.add_tool_result("c2", "84")
    session.document_context = "Doc 3"
    req_tool2 = session.messages_for_request()

    assert req_tool2[-1]["role"] == "tool"
    assert req_tool2[-1]["tool_call_id"] == "c2"
    assert isinstance(req_tool2[-1]["content"], str)
    assert snapshot_text("Doc 3") in req_tool2[-1]["content"]
    assert session.messages[-1]["content"] == "84"
    # Shared prefix (all messages before the last tool result) is byte-identical across rounds
    assert req_tool2[: len(req_tool1) - 1] == req_tool1[:-1]

    # After those calls, session.messages has no SNAPSHOT_HEADER, date line, or memory block
    for m in session.messages:
        content_str = str(m.get("content") or "")
        assert SNAPSHOT_HEADER not in content_str
        assert "Today's date is" not in content_str
        assert "[USER PROFILE / MEMORY]" not in content_str

    # A mock DB's add_message / replace_messages were not called with that header
    for call in mock_db.add_message.call_args_list:
        for arg in call.args:
            assert SNAPSHOT_HEADER not in str(arg)
            assert "Today's date is" not in str(arg)
            assert "[USER PROFILE / MEMORY]" not in str(arg)
    for call in mock_db.replace_messages.call_args_list:
        for arg in call.args:
            assert SNAPSHOT_HEADER not in str(arg)
            assert "Today's date is" not in str(arg)
            assert "[USER PROFILE / MEMORY]" not in str(arg)


def test_messages_for_request_multimodal_user_list_unchanged():
    session = ChatSession(system_prompt="Base system")
    multimodal_content = [
        {"type": "text", "text": "Describe this image:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,123"}},
    ]
    session.add_user_message(multimodal_content)
    session.document_context = "Doc 1"

    req1 = session.messages_for_request()
    assert req1[1]["role"] == "user"
    assert len(req1[1]["content"]) == 3
    assert req1[1]["content"][0] == multimodal_content[0]
    assert req1[1]["content"][1] == multimodal_content[1]
    assert snapshot_text("Doc 1") in req1[1]["content"][2]["text"]

    session.add_assistant_message("This is a chart.")
    session.add_user_message("Second question")
    req2 = session.messages_for_request()

    # The stored multimodal user list is unchanged on the next request
    assert req2[1]["content"] == multimodal_content
    assert session.messages[1]["content"] == multimodal_content
    # Second turn has its own folded tail
    assert req2[-1]["role"] == "user"
    assert req2[-1]["content"][0]["text"] == "Second question"


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

