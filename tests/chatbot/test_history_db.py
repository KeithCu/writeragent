"""Unit tests for JSON chat history (no LibreOffice)."""

from __future__ import annotations

import json
import os

import pytest

from plugin.chatbot.history_db import JSONHistory


def test_json_history_roundtrip_and_no_temp_leftovers(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", "Hello JSON!")
    history.add_message("assistant", "Thinking...")
    assert history.get_messages() == [
        {"role": "user", "content": "Hello JSON!", "tool_calls": None},
        {"role": "assistant", "content": "Thinking...", "tool_calls": None},
    ]
    leftovers = [name for name in os.listdir(history.history_dir) if name.startswith(".history-")]
    assert leftovers == []


def test_json_history_refuses_to_replace_unreadable_file(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", "keep")
    with open(history.file_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    corrupt = open(history.file_path, encoding="utf-8").read()

    with pytest.raises(json.JSONDecodeError):
        history.get_messages()
    history.add_message("user", "new")

    assert open(history.file_path, encoding="utf-8").read() == corrupt
