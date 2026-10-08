# WriterAgent tests
from plugin.chatbot.panel import ChatSession
from plugin.chatbot.web_research_chat import (
    display_name_for_path_or_name,
    document_open_preview_line,
    document_open_step_chat_text,
    format_sub_agent_conversation_history,
)
import pytest


def test_format_sub_agent_history_empty_session():
    session = ChatSession(system_prompt="Observe web search.")
    assert format_sub_agent_conversation_history(session) == ""


def test_format_sub_agent_history_excludes_current_query():
    session = ChatSession(system_prompt="Observe web search.")
    session.messages.append({"role": "user", "content": "follow up question"})
    assert format_sub_agent_conversation_history(session, current_query="follow up question") == ""


def test_format_sub_agent_history_includes_prior_turns_excludes_current():
    session = ChatSession(system_prompt="Observe web search.")
    session.messages.append({"role": "user", "content": "price of inception mercury 2?"})
    session.messages.append({"role": "assistant", "content": "about $500"})
    session.messages.append({"role": "user", "content": "you said that earlier"})
    history = format_sub_agent_conversation_history(session, current_query="you said that earlier")
    assert "price of inception mercury 2?" in history
    assert "about $500" in history
    assert "you said that earlier" not in history


def test_format_sub_agent_history_skips_system_and_tool():
    session = ChatSession(system_prompt="Observe web search.")
    session.messages.append({"role": "tool", "tool_call_id": "x", "content": "result"})
    session.messages.append({"role": "user", "content": "hello"})
    history = format_sub_agent_conversation_history(session, current_query="hello")
    assert "result" not in history
    assert history == ""


def test_format_sub_agent_history_strips_html():
    session = ChatSession(system_prompt="Observe")
    session.messages.append({"role": "user", "content": "hello <strong>bold</strong>"})
    session.messages.append({"role": "assistant", "content": "how <em>are</em> you?"})
    history = format_sub_agent_conversation_history(session)
    assert "<strong>" not in history
    assert "bold" in history
    assert "<em>" not in history
    assert "are" in history


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("/tmp/Budget_2026.ods", "Budget_2026.ods", id="test_display_name_uses_basename_for_absolute_path"),
        pytest.param("Budget.ods", "Budget.ods", id="test_display_name_keeps_relative_name"),
    ],
)
def test_display_name_uses_basename_for_absolute_path(value, expected):
    assert display_name_for_path_or_name(value) == expected

def test_step_zero_tool_and_preview_only():
    q = "/tmp/Budget_2026.ods"
    text = document_open_step_chat_text(q, 0)
    assert "Tool: delegate_read_document" in text
    assert "Budget_2026.ods" in text
    assert "read-only" in text.lower()
    assert "[Document research]" not in text
    assert "[Additional document research]" not in text


def test_step_one_same_format_as_first():
    q = "/tmp/Brief.odt"
    first = document_open_step_chat_text(q, 0)
    second = document_open_step_chat_text(q, 1)
    assert first == second
    assert "[Additional document research]" not in second
    assert "[Document research]" not in second


def test_step_index_negative_treated_as_first():
    q = "Budget.ods"
    assert document_open_step_chat_text(q, -1) == document_open_step_chat_text(q, 0)


def test_document_open_preview_line():
    line = document_open_preview_line("/tmp/foo.ods")
    assert "foo.ods" in line
    assert "read-only" in line.lower()
