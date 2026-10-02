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
        {"role": "user", "content": "Hello JSON!"},
        {"role": "assistant", "content": "Thinking..."},
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


def test_json_history_reraises_oserror(tmp_path, monkeypatch):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", "keep")

    real_open = open

    def _boom(*args, **kwargs):
        if args and args[0] == history.file_path and args[1:][:1] == ("r",):
            raise OSError("disk")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", _boom)
    with pytest.raises(OSError):
        history.get_messages()


def test_json_history_rejects_non_list(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    with open(history.file_path, "w", encoding="utf-8") as handle:
        handle.write("{}")
    with pytest.raises(json.JSONDecodeError):
        history.get_messages()
    history.add_message("user", "new")
    assert open(history.file_path, encoding="utf-8").read() == "{}"


def test_message_to_dict_omits_null_tool_calls_and_marks_images():
    from plugin.chatbot.history_db import message_to_dict

    text = message_to_dict("user", "hello")
    assert text == {"role": "user", "content": "hello"}
    calls = [{"id": "c1"}]
    assert message_to_dict("assistant", "hi", tool_calls=calls)["tool_calls"] == calls
    marked = message_to_dict("user", [
        {"type": "text", "text": "see"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    assert marked["content"] == "see [Image Attached]"
    assert "AAAA" not in marked["content"]


def test_sqlite_skips_undecodable_row(tmp_path):
    from plugin.chatbot.history_db import SQLite3History
    import sqlite3

    db_path = str(tmp_path / "history.db")
    history = SQLite3History("sid", db_path)
    history.add_message("user", "keep")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
            ("sid", "{not json"),
        )
        conn.commit()
    history.add_message("assistant", "also")
    loaded = history.get_messages()
    assert [row["content"] for row in loaded] == ["keep", "also"]
    assert all("tool_calls" not in row for row in loaded)
