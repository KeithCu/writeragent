# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for plugin.contrib.smolagents.models."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def test_get_tool_json_schema_normalizes_nullable_scalars():
    """Verify smolagents get_tool_json_schema normalizes optional parameters to ['type', 'null']."""
    from plugin.contrib.smolagents.models import get_tool_json_schema
    from plugin.contrib.smolagents.tools import Tool

    class DummyImageTool(Tool):
        name = "image_generate"
        description = "Generate an image"
        inputs = {
            "prompt": {"type": "string", "description": "The prompt", "nullable": False},
            "source_image": {"type": "string", "description": "Optional image", "nullable": True},
        }
        output_type = "string"
        skip_forward_signature_validation = True

        def forward(self, prompt: str, source_image: str | None = None) -> str:
            return "done"

    schema = get_tool_json_schema(DummyImageTool())
    fn = schema["function"]
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["prompt"]
    assert params["properties"]["prompt"]["type"] == "string"
    assert params["properties"]["source_image"]["type"] == ["string", "null"]
    assert "nullable" not in params["properties"]["source_image"]


def test_tool_call_observation_json_for_dict_and_list():
    from plugin.contrib.smolagents.agents import ToolCallingAgent, ToolOutput
    from plugin.contrib.smolagents.memory import ActionStep
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.contrib.smolagents.tools import Tool

    results: dict[str, Any] = {}

    class CustomTool(Tool):
        name = "custom_tool"
        description = "custom"
        inputs = {"key": {"type": "string", "description": "k", "nullable": False}}
        output_type = "any"
        skip_forward_signature_validation = True

        def forward(self, key: str) -> Any:
            return results.get(key)

    tool = CustomTool()
    agent = ToolCallingAgent(tools=[tool], model=MagicMock(), final_answer_tool_name="final_answer")

    def run_call(call_id: str, key_arg: str) -> ToolOutput:
        tc = ChatMessageToolCall(
            function=ChatMessageToolCallFunction(name="custom_tool", arguments={"key": key_arg}),
            id=call_id,
            type="function",
        )
        msg = ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[tc])
        outputs = list(agent.process_tool_calls(msg, ActionStep(step_number=1, timing=MagicMock())))
        tool_outs = [o for o in outputs if isinstance(o, ToolOutput)]
        return tool_outs[0]

    # Dict -> JSON
    dict_val = {"status": "ok", "items": [1, 2]}
    results["d"] = dict_val
    out_dict = run_call("c1", "d")
    assert out_dict.observation == '{"status": "ok", "items": [1, 2]}'
    assert out_dict.output == dict_val

    # List -> JSON
    list_val = ["apple", "banana"]
    results["l"] = list_val
    out_list = run_call("c2", "l")
    assert out_list.observation == '["apple", "banana"]'
    assert out_list.output == list_val

    # bool True -> str "True" (not lowercase true)
    results["b"] = True
    out_bool = run_call("c3", "b")
    assert out_bool.observation == "True"
    assert out_bool.output is True

    # None -> str "None"
    results["n"] = None
    out_none = run_call("c4", "n")
    assert out_none.observation == "None"
    assert out_none.output is None
