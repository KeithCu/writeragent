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

import hashlib
import logging
import json
import os
import tempfile
from contextlib import closing
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
    # Don't persist MBs of base64 audio or images to history db.
    if isinstance(content, list):
        text_parts = []
        has_audio = False
        has_image = False
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    # A present "text" of None, or any non-str, used to reach
                    # " ".join and raise TypeError here. add_message calls
                    # this before its try, so the turn never saved. str(... or
                    # "") is always a str (None and missing become "").
                    text_parts.append(str(item.get("text") or ""))
                elif item.get("type") == "input_audio":
                    has_audio = True
                elif item.get("type") == "image_url":
                    has_image = True
        content = " ".join(text_parts)
        if has_audio:
            content = f"{content} [Audio Attached]" if content else "[Audio Attached]"
        if has_image:
            # A vision turn used to reload as ordinary text, with the image
            # parts dropped and no marker that one had been attached.
            content = f"{content} [Image Attached]" if content else "[Image Attached]"

    row: dict[str, Any] = {"role": role, "content": content}
    # tool_calls: null on user and system rows makes strict
    # OpenAI-compatible servers reject the transcript after a restart.
    if tool_calls is not None:
        row["tool_calls"] = tool_calls
    return row


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
        # sqlite3.Connection's context manager commits or rolls back and does
        # not close. Each history call left a connection until GC. closing()
        # calls close() on the way out, including when execute raises. Every
        # connect in this file uses the same wrap.
        with closing(sqlite3.connect(self.db_path)) as conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO message_store (session_id, message) VALUES (?, ?)", (self.session_id, json.dumps(msg_dict)))
            conn.commit()
            log.info(f"SQLite3: Added message for session {self.session_id}")

    def get_messages(self) -> list[dict[str, Any]]:
        assert sqlite3 is not None
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute("SELECT id, message FROM message_store WHERE session_id = ? ORDER BY id ASC", (self.session_id,))
            msgs: list[dict[str, Any]] = []
            for row_id, raw in cursor.fetchall():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    # One torn row used to raise out of the whole session, so
                    # later turns inserted and then vanished on the next open.
                    log.exception("SQLite3: skipping undecodable message id=%s session=%s", row_id, self.session_id)
                    continue
                if not isinstance(parsed, dict):
                    log.error("SQLite3: skipping non-object message id=%s session=%s", row_id, self.session_id)
                    continue
                msgs.append(parsed)
            log.debug(f"SQLite3: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs

    def clear(self) -> None:
        assert sqlite3 is not None
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (self.session_id,))
            conn.commit()

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace this session's rows in one transaction.

        A delete that commits before the inserts leaves an empty session if a
        later insert fails. Other session ids in the same file are untouched.
        """
        assert sqlite3 is not None
        with closing(sqlite3.connect(self.db_path)) as conn:
            try:
                conn.execute("DELETE FROM message_store WHERE session_id = ?", (self.session_id,))
                for msg in messages:
                    conn.execute(
                        "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                        (self.session_id, json.dumps(msg)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _json_history_filename(session_id: str, history_dir: str) -> str:
    """One path segment under the history directory.

    Regenerated ids are SHA-256 hex or UUID. Unsafe ids are hashed for the filename only.
    To stop case-alias collisions on case-insensitive filesystems, any id that contains
    an uppercase letter is also hashed, unless an older unhashed file already exists.
    """
    name = session_id or ""
    is_unsafe = (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or os.path.basename(name) != name
    )
    if is_unsafe:
        name = hashlib.sha256(name.encode("utf-8")).hexdigest()
    elif name != name.lower():
        hashed_name = hashlib.sha256(name.encode("utf-8")).hexdigest()
        exact_match_found = False
        if history_dir:
            try:
                exact_match_found = f"{name}.json" in os.listdir(history_dir)
            except OSError:
                pass
        if exact_match_found:
            pass
        else:
            name = hashed_name
    return f"{name}.json"


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

        self.file_path = os.path.join(self.history_dir, _json_history_filename(session_id, self.history_dir))

    def add_message(self, role: str, content: Any, tool_calls: Any = None) -> None:
        msg_dict = message_to_dict(role, content, tool_calls)
        try:
            messages = self.get_messages()
        except (json.JSONDecodeError, UnicodeDecodeError):
            # open(..., "w") truncated the file before json.dump. A crash or a
            # bad read then looked like an empty session and the next add
            # replaced history with one row. Leave an unreadable file alone.
            log.exception("JSONHistory: refusing to overwrite unreadable session %s", self.session_id)
            return
        messages.append(msg_dict)
        try:
            self._replace_messages(messages)
            log.info(f"JSONHistory: Added message for session {self.session_id}")
        except (OSError, TypeError):
            # The turn is already in ChatSession.messages. Swallowing the save
            # left the next open without that row. get_messages already re-raises.
            log.exception("JSONHistory: Error saving message")
            raise

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
            if not isinstance(msgs, list) or any(not isinstance(item, dict) for item in msgs):
                # A JSON object or a bare null used to become session.messages
                # and then throw on append, or look empty and get overwritten.
                log.error("JSONHistory: session %s is not a list of objects", self.session_id)
                raise json.JSONDecodeError("session is not a list of objects", "", 0)
            log.debug(f"JSONHistory: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Callers (ChatSession open, add_message) must not treat a corrupt
            # file as an empty history and write a fresh system row over it.
            log.exception("JSONHistory: Error reading messages")
            raise
        except OSError:
            # An OSError used to return [] and the next add_message replaced
            # the file. Same refusal as a decode error: leave the file alone.
            log.exception("JSONHistory: Error reading messages")
            raise

    def clear(self) -> None:
        self.replace_messages([])

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace this session file in one ``os.replace``.

        ``clear`` used to swallow an ``os.remove`` ``OSError`` and leave the old
        rows, so a later append duplicated them. Both now write the file in one
        ``os.replace`` or raise.
        """
        self._replace_messages(list(messages))


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
        # Fall back to JSON only when there is no database file yet.
        # connect() already waits on a lock. A sqlite error on an existing
        # writeragent_history.db must not open *.db.d/ and stay there.
        if os.path.isfile(db_path):
            log.exception("SQLite failed for existing history database %s", db_path)
            raise
        log.exception("SQLite failed, falling back to JSON")
        return JSONHistory(session_id, db_path)
