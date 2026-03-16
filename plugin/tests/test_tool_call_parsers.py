import json
import pytest
from plugin.contrib.tool_call_parsers import (
    get_parser,
    get_parser_for_model,
)

def test_hermes_parser():
    parser = get_parser("hermes")
    text = 'Hello\n<tool_call>{"name": "test_tool", "arguments": {"cmd": "ls"}}</tool_call>'
    content, tool_calls = parser.parse(text)
    
    assert content == "Hello"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "test_tool"

def test_hermes_parser_unclosed():
    parser = get_parser("hermes")
    text = '<tool_call>{"name": "test_tool", "arguments": {"cmd": "ls"}'
    content, tool_calls = parser.parse(text)
    
    # Hermes returns None on JSONDecodeError from unclosed JSON
    assert tool_calls is None

def test_deepseek_v3_parser():
    parser = get_parser("deepseek_v3")
    text = (
        'Thinking...\n'
        '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>get_weather\n'
        '```json\n{"city": "Paris"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
    )
    content, tool_calls = parser.parse(text)
    
    assert content == "Thinking..."
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_mistral_parser_v11():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS]get_weather{"city": "Berlin"}'
    content, tool_calls = parser.parse(text)
    
    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_mistral_parser_prev11():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS] [{"name": "get_weather", "arguments": {"city": "Berlin"}}]'
    content, tool_calls = parser.parse(text)
    
    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_llama_parser():
    parser = get_parser("llama3_json")
    text = 'Output: <|python_tag|>\n{"name": "calc", "arguments": {"expr": "2+2"}}'
    content, tool_calls = parser.parse(text)
    
    assert content == "Output:"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "calc"

def test_get_parser_for_model():
    p1 = get_parser_for_model("hermes-2-pro")
    assert p1 is not None
    # We can check it works instead of strict isinstance on the inner class
    _, tc = p1.parse('<tool_call>{"name": "test", "arguments": {}}</tool_call>')
    assert tc is not None
    
    p2 = get_parser_for_model("deepseek-coder-v3")
    assert p2 is not None
    
    p3 = get_parser_for_model("mistral-large")
    assert p3 is not None
    
    p4 = get_parser_for_model("llama-3-70b")
    assert p4 is not None
    
    assert get_parser_for_model("unknown") is None
