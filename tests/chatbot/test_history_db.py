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


def test_message_to_dict_null_or_non_str_text_part():
    """A present text key of None used to TypeError inside " ".join before add_message's try."""
    from plugin.chatbot.history_db import message_to_dict

    row = message_to_dict("user", [
        {"type": "text", "text": None},
        {"type": "text", "text": "keep"},
        {"type": "text"},
        {"type": "text", "text": 12},
    ])
    assert row["content"] == " keep  12"


def test_json_history_hashes_session_ids(tmp_path):
    import hashlib
    from plugin.chatbot.history_db import _json_history_filename

    db_path = str(tmp_path / "writeragent_history.db")
    history_dir = db_path + ".d"
    os.makedirs(history_dir, exist_ok=True)

    # 1. Unsafe ids are hashed.
    history = JSONHistory("../outside", db_path)
    assert os.path.dirname(history.file_path) == history.history_dir
    assert os.path.basename(history.file_path) == _json_history_filename("../outside", history.history_dir)
    assert "/" not in os.path.basename(history.file_path)
    assert os.path.basename(history.file_path) != "../outside.json"

    # 2. Safe plain lowercase ids are NOT hashed.
    plain = JSONHistory("session_abc", db_path)
    assert plain.file_path.endswith("session_abc.json")

    # 3. Case variants do not share a file (one hashes, one does not).
    plain_A = JSONHistory("session_A", db_path)
    plain_a = JSONHistory("session_a", db_path)
    assert plain_A.file_path != plain_a.file_path
    assert plain_a.file_path.endswith("session_a.json")
    expected_hash_A = hashlib.sha256(b"session_A").hexdigest()
    assert plain_A.file_path.endswith(f"{expected_hash_A}.json")

    # 4. If an old safe uppercase file exists, we fall back to it.
    open(os.path.join(history_dir, "session_B.json"), "w").close()
    plain_B = JSONHistory("session_B", db_path)
    assert plain_B.file_path.endswith("session_B.json")

    # 5. On a case-insensitive FS, the check must be exact.
    # We simulate this by checking that if only 'session_c.json' is present,
    # requesting 'session_C' produces a hashed filename (it should not wrongly
    # fall back to 'session_C.json' just because a case-insensitive exists() returns True).
    open(os.path.join(history_dir, "session_c.json"), "w").close()
    plain_C = JSONHistory("session_C", db_path)
    expected_hash_C = hashlib.sha256(b"session_C").hexdigest()
    assert plain_C.file_path.endswith(f"{expected_hash_C}.json")


def test_json_history_refuses_to_replace_invalid_utf8_file(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", "keep")
    with open(history.file_path, "wb") as handle:
        handle.write(b"\xff\xff")
    corrupt = open(history.file_path, "rb").read()

    with pytest.raises(UnicodeDecodeError):
        history.get_messages()
    history.add_message("user", "new")

    assert open(history.file_path, "rb").read() == corrupt


def test_json_history_reraises_save_oserror(tmp_path, monkeypatch):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))

    def _boom(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr("plugin.chatbot.history_db.os.replace", _boom)
    with pytest.raises(OSError):
        history.add_message("user", "new")


def test_existing_sqlite_db_does_not_fall_back_to_json(tmp_path, monkeypatch):
    import sqlite3

    from plugin.chatbot import history_db

    db_path = tmp_path / "writeragent_history.db"
    db_path.write_text("not a database", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(history_db.sqlite3, "connect", _boom)
    with pytest.raises(sqlite3.OperationalError):
        history_db.get_chat_history("sid", str(db_path))
    assert not (tmp_path / "writeragent_history.db.d").exists()


def test_json_history_saves_null_text_part(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", [{"type": "text", "text": None}, {"type": "text", "text": "keep"}])
    assert history.get_messages()[0]["content"] == " keep"


def test_sqlite_closes_connection_and_saves_null_text(tmp_path, monkeypatch):
    import sqlite3

    from plugin.chatbot import history_db

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def _wrap(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(history_db.sqlite3, "connect", _wrap)
    history = history_db.SQLite3History("sid", str(tmp_path / "history.db"))
    history.add_message("user", [{"type": "text", "text": None}])
    assert history.get_messages()[0]["content"] == ""
    history.clear()
    # init, add, get, clear — each connect must be closed, not left to GC.
    assert len(opened) == 4
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


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


def test_json_history_replace_messages_replaces_the_file(tmp_path):
    history = JSONHistory("session_abc", str(tmp_path / "writeragent_history.db"))
    history.add_message("user", "old")
    history.add_message("assistant", "older", tool_calls=[{"id": "call-1"}])
    history.replace_messages([{"role": "user", "content": "next", "tool_calls": [{"id": "call-2"}]}])
    assert history.get_messages() == [{"role": "user", "content": "next", "tool_calls": [{"id": "call-2"}]}]
    leftovers = [name for name in os.listdir(history.history_dir) if name.startswith(".history-")]
    assert leftovers == []


def test_sqlite_history_replace_messages_keeps_other_sessions(tmp_path):
    from plugin.chatbot.history_db import SQLite3History

    db_path = str(tmp_path / "history.db")
    history = SQLite3History("sid", db_path)
    other = SQLite3History("other", db_path)
    history.add_message("user", "old")
    other.add_message("user", "keep")
    history.replace_messages([{"role": "assistant", "content": "next", "tool_calls": [{"id": "call-2"}]}])
    assert history.get_messages() == [{"role": "assistant", "content": "next", "tool_calls": [{"id": "call-2"}]}]
    assert other.get_messages() == [{"role": "user", "content": "keep"}]
    history.replace_messages([])
    assert history.get_messages() == []
    assert other.get_messages() == [{"role": "user", "content": "keep"}]
