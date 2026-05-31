# WriterAgent tests
from plugin.chatbot.panel import ChatSession
from plugin.chatbot.web_research_chat import format_sub_agent_conversation_history


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
