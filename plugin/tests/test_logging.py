import pytest
import os
import json

from plugin.framework.logging import (
    format_tool_call_for_display,
    format_tool_result_for_display,
    update_activity_state,
    _activity_state
)

def test_format_tool_call_for_display():
    assert format_tool_call_for_display("my_tool", {"arg": "val"}) == "my_tool(arg='val')"
    assert "..." in format_tool_call_for_display("my_tool", {"arg": "a"*200})
    assert format_tool_call_for_display(None, None, method="GET") == "GET"

def test_format_tool_result_for_display():
    res = format_tool_result_for_display("my_tool", "plain text")
    assert res == "my_tool() -> 'plain text'"

    # Text in json blocks
    res = format_tool_result_for_display("my_tool", json.dumps({"content": [{"type": "text", "text": "inner text"}]}))
    assert "inner text" in res

    # With args
    res = format_tool_result_for_display("my_tool", "res", args={"k": "v"})
    assert res == "my_tool(k='v') -> 'res'"

def test_update_activity_state():
    update_activity_state("phase1", round_num=1, tool_name="tool1")
    assert _activity_state["phase"] == "phase1"
    assert _activity_state["round_num"] == 1
    assert _activity_state["tool_name"] == "tool1"
    assert _activity_state["last_activity"] > 0
