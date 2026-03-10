import pytest
import os
import json

from plugin.framework.history_db import message_to_dict

def test_message_to_dict_text():
    res = message_to_dict("user", "hello")
    assert res["role"] == "user"
    assert res["content"] == "hello"
    assert res["tool_calls"] is None

def test_message_to_dict_list():
    res = message_to_dict("user", [{"type": "text", "text": "hello"}, {"type": "input_audio"}])
    assert res["role"] == "user"
    assert "hello" in res["content"]
    assert "[Audio Attached]" in res["content"]
