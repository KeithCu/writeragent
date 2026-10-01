# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

import logging
import json
import os
import tempfile
from typing import Any

try:
    import sqlite3

    HAS_SQLITE = True
except ImportError:
    sqlite3 = None  # type: ignore
    HAS_SQLITE = False

from plugin.framework.config import user_config_dir

log = logging.getLogger(__name__)


def _get_db_path() -> str:
    config_dir = user_config_dir()
    if config_dir:
        try:
            if not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)
        except OSError:
            log.exception("Error creating config directory")
        path = os.path.join(config_dir, "writeragent_history.db")
        log.info(f"Using database path: {path}")
        return path
    return "writeragent_history.db"


# LangChain-compatible JSON conversion
def message_to_dict(role: str, content: Any, tool_calls: Any = None) -> dict[str, Any]:
    # Don't persist MBs of base64 audio to history db.
    if isinstance(content, list):
        text_parts = []
        has_audio = False
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "input_audio":
                    has_audio = True
        content = " ".join(text_parts)
        if has_audio:
            if content:
                content += " [Audio Attached]"
            else:
                content = "[Audio Attached]"

    return {"role": role, "content": content, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Native SQLite3 Implementation
# ---------------------------------------------------------------------------
class SQLite3History:
    session_id: str
    db_path: str

    def __init__(self, session_id: str, db_path: str) -> None:
        self.session_id = session_id
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        assert sqlite3 is not None
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_store (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    message TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON message_store(session_id)")
            conn.commit()

    def add_message(self, role: str, content: Any, tool_calls: Any = None) -> None:
        assert sqlite3 is not None
        msg_dict = message_to_dict(role, content, tool_calls)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO message_store (session_id, message) VALUES (?, ?)", (self.session_id, json.dumps(msg_dict)))
            conn.commit()
            log.info(f"SQLite3: Added message for session {self.session_id}")

    def get_messages(self) -> list[dict[str, Any]]:
        assert sqlite3 is not None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT message FROM message_store WHERE session_id = ? ORDER BY id ASC", (self.session_id,))
            msgs = [json.loads(row[0]) for row in cursor.fetchall()]
            log.debug(f"SQLite3: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs

    def clear(self) -> None:
        assert sqlite3 is not None
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (self.session_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# JSON Implementation (Fallback)
# ---------------------------------------------------------------------------
class JSONHistory:
    session_id: str
    history_dir: str
    file_path: str

    def __init__(self, session_id: str, db_path: str) -> None:
        self.session_id = session_id
        # Use a directory based on the db_path filename (e.g. writeragent_history.json.d/)
        self.history_dir = db_path + ".d"
        try:
            if not os.path.exists(self.history_dir):
                os.makedirs(self.history_dir, exist_ok=True)
            log.info(f"JSONHistory: Using directory {self.history_dir}")
        except OSError:
            log.exception("JSONHistory: Error creating directory")

        self.file_path = os.path.join(self.history_dir, f"{session_id}.json")

    def add_message(self, role: str, content: Any, tool_calls: Any = None) -> None:
        msg_dict = message_to_dict(role, content, tool_calls)
        try:
            messages = self.get_messages()
        except json.JSONDecodeError:
            # open(..., "w") truncated the file before json.dump. A crash or a
            # bad read then looked like an empty session and the next add
            # replaced history with one row. Leave an unreadable file alone.
            log.exception("JSONHistory: refusing to overwrite unreadable session %s", self.session_id)
            return
        messages.append(msg_dict)
        try:
            self._replace_messages(messages)
            log.info(f"JSONHistory: Added message for session {self.session_id}")
        except (OSError, IOError, TypeError):
            log.exception("JSONHistory: Error saving message")

    def _replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Atomic replace in the session directory (same pattern as MemoryStore.write)."""
        directory = self.history_dir or os.path.dirname(self.file_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".history-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(messages, handle, indent=2)
            os.replace(tmp_path, self.file_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_messages(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            log.debug(f"JSONHistory: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs
        except json.JSONDecodeError:
            # Callers (ChatSession open, add_message) must not treat a corrupt
            # file as an empty history and write a fresh system row over it.
            log.exception("JSONHistory: Error reading messages")
            raise
        except (OSError, IOError):
            log.exception("JSONHistory: Error reading messages")
            return []

    def clear(self) -> None:
        if os.path.exists(self.file_path):
            try:
                os.remove(self.file_path)
            except OSError:
                log.exception("JSONHistory: Error clearing history")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_chat_history(session_id: str, db_path: str | None = None) -> SQLite3History | JSONHistory:
    if not db_path:
        db_path = _get_db_path()

    if not HAS_SQLITE:
        log.warning("SQLite not available; using JSON fallback for chat history")
        return JSONHistory(session_id, db_path)
    assert sqlite3 is not None
    try:
        log.info(f"Using SQLite for chat history at {db_path}")
        return SQLite3History(session_id, db_path)
    except sqlite3.Error:
        log.exception("SQLite failed, falling back to JSON")
        return JSONHistory(session_id, db_path)
