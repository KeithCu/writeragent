# WriterAgent tests — web research step budget injection
from __future__ import annotations

from unittest.mock import MagicMock

from plugin.contrib.smolagents.models import ChatMessage, MessageRole
from plugin.modules.chatbot.web_research import WebResearchToolCallingAgent


def test_web_research_augment_messages_includes_used_and_remaining():
    model = MagicMock()
    agent = WebResearchToolCallingAgent(tools=[], model=model, max_steps=10)
    agent.step_number = 3
    base = [ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": "prior"}])]
    out = agent.augment_messages_for_step(base)
    assert len(out) == 2
    last = out[-1]
    assert last.role == MessageRole.USER
    text = str(last.content)
    assert "2 step(s) used" in text
    assert "8 step(s) remaining" in text
    assert "maximum 10" in text


def test_tool_calling_agent_default_augment_is_identity():
    from plugin.contrib.smolagents.agents import ToolCallingAgent

    model = MagicMock()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=5)
    base = [ChatMessage(role=MessageRole.SYSTEM, content=[{"type": "text", "text": "sys"}])]
    assert agent.augment_messages_for_step(base) is base
