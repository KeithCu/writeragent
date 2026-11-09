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
import json
import logging
import os
import threading
from contextlib import closing, suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.config import atomic_write_text, user_config_dir

try:
    import sqlite3

    HAS_SQLITE = True
except ImportError:
    sqlite3 = None  # type: ignore
    HAS_SQLITE = False

log = logging.getLogger(__name__)


def _get_db_path() -> str:
    config_dir = user_config_dir()
    if config_dir:
        try:
            os.makedirs(config_dir, exist_ok=True)
        except OSError:
            log.exception("Error creating config directory")
        path = os.path.join(config_dir, "writeragent_history.db")
        log.info(f"Using database path: {path}")
        return path
    return "writeragent_history.db"


# Local {role, content, tool_calls?} row for SQLite and JSON.
def message_to_dict(role: str, content: Any, tool_calls: Any = None) -> dict[str, Any]:
    # Don't persist MBs of base64 audio or images to history db.
    if isinstance(content, list):
        from plugin.chatbot.chat_session import message_text

        text = message_text(content)
        kinds: set[str] = set()
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in ("input_audio", "image_url"):
                    kinds.add(t)
        markers = []
        if "input_audio" in kinds:
            markers.append("[Audio Attached]")
        if "image_url" in kinds:
            # Vision turns persist an [Image Attached] marker when image parts are stripped.
            markers.append("[Image Attached]")
        content = " ".join(p for p in [text, *markers] if p)

    row: dict[str, Any] = {"role": role, "content": content}
    # tool_calls: null on user and system rows makes strict
    # OpenAI-compatible servers reject the transcript after a restart.
    if tool_calls is not None:
        row["tool_calls"] = tool_calls
    return row


# ---------------------------------------------------------------------------
# Native SQLite3 Implementation
# ---------------------------------------------------------------------------
_keepers: dict[str, Any] = {}
_keepers_lock = threading.Lock()


def reset_history_keepers_for_tests() -> None:
    """Close and clear keeper connection(s) (for test isolation)."""
    with _keepers_lock:
        for conn in _keepers.values():
            with suppress(Exception):
                conn.close()
        _keepers.clear()


def _open_wal(db_path: str, **kwargs: Any) -> Any:
    assert sqlite3 is not None
    conn = sqlite3.connect(db_path, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


def _ensure_keeper(db_path: str) -> None:
    if db_path in _keepers:
        return
    with _keepers_lock:
        if db_path not in _keepers:
            # A connection attaches to the WAL only once it has read the database,
            # and connect or SELECT 1 does not read it. Setting the pragmas joins
            # the WAL index; the keeper is never used after it opens.
            _keepers[db_path] = _open_wal(db_path, check_same_thread=False)


class SQLite3History:
    session_id: str
    db_path: str

    def __init__(self, session_id: str, db_path: str) -> None:
        self.session_id = session_id
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> closing[Any]:
        # Default FULL fsyncs every UI/drain commit; WAL persists across connections,
        # but synchronous does not, so both pragmas run on every connect.
        # SQLite checkpoints and unlinks the WAL when its last connection closes;
        # holding a process-lifetime keeper connection per db_path prevents that,
        # so commits append to the WAL without a checkpoint.
        # WAL history assumes a single host at a time (multi-host profile sharing is unsupported).
        # WAL+NORMAL is process-crash consistent and a power loss can drop uncheckpointed commits.
        # sqlite3.Connection's context manager commits or rolls back and does
        # not close. Each history call left a connection until GC. closing()
        # calls close() on the way out, including when execute raises. Every
        # per-call connect in this file uses the same wrap.
        conn = _open_wal(self.db_path)
        try:
            _ensure_keeper(self.db_path)
        except BaseException:
            conn.close()
            raise
        return closing(conn)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_store (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    message TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON message_store(session_id)")

    def _insert_message(self, conn: Any, msg: dict[str, Any]) -> None:
        conn.execute("INSERT INTO message_store (session_id, message) VALUES (?, ?)", (self.session_id, json.dumps(msg)))

    def add_message(self, role: str, content: Any, tool_calls: Any = None) -> None:
        msg_dict = message_to_dict(role, content, tool_calls)
        with self._connect() as conn:
            self._insert_message(conn, msg_dict)
            conn.commit()
            log.debug(f"SQLite3: Added message for session {self.session_id}")

    def update_last_user_content(self, content: Any) -> None:
        msg_dict = message_to_dict("user", content)
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, message FROM message_store WHERE session_id = ? ORDER BY id DESC",
                (self.session_id,),
            )
            for row_id, raw in cursor.fetchall():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and parsed.get("role") == "user":
                        conn.execute(
                            "UPDATE message_store SET message = ? WHERE id = ?",
                            (json.dumps(msg_dict), row_id),
                        )
                        break
                except json.JSONDecodeError:
                    continue
            else:
                self._insert_message(conn, msg_dict)
            conn.commit()

    def get_messages(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT id, message FROM message_store WHERE session_id = ? ORDER BY id ASC", (self.session_id,))
            msgs: list[dict[str, Any]] = []
            for row_id, raw in cursor.fetchall():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    # Skip undecodable rows so one corrupt JSON blob does not drop the whole session.
                    log.exception("SQLite3: skipping undecodable message id=%s session=%s", row_id, self.session_id)
                    continue
                if not isinstance(parsed, dict):
                    log.error("SQLite3: skipping non-object message id=%s session=%s", row_id, self.session_id)
                    continue
                msgs.append(parsed)
            log.debug(f"SQLite3: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace this session's rows in one transaction.

        A delete that commits before the inserts leaves an empty session if a
        later insert fails. Other session ids in the same file are untouched.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (self.session_id,))
            for msg in messages:
                self._insert_message(conn, msg)
            conn.commit()


def _json_history_filename(session_id: str, history_dir: str) -> str:
    """One path segment under the history directory.

    Regenerated ids are SHA-256 hex or UUID. Unsafe ids are hashed for the filename only.
    To stop case-alias collisions on case-insensitive filesystems, any id that contains
    an uppercase letter is also hashed, unless an older unhashed file already exists.
    """
    name = session_id
    is_unsafe = (
        not name
        or name in (".", "..")
        or "\\" in name
        or os.path.basename(name) != name
    )
    legacy = False
    if not is_unsafe and name != name.lower():
        try:
            legacy = f"{name}.json" in os.listdir(history_dir)
        except OSError:
            pass
    if is_unsafe or (name != name.lower() and not legacy):
        name = hashlib.sha256(name.encode("utf-8")).hexdigest()
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
            os.makedirs(self.history_dir, exist_ok=True)
            log.info(f"JSONHistory: Using directory {self.history_dir}")
        except OSError:
            log.exception("JSONHistory: Error creating directory")

        self.file_path = os.path.join(self.history_dir, _json_history_filename(session_id, self.history_dir))

    def _modify(self, mutate: Callable[[list[dict[str, Any]]], None], error: str) -> None:
        try:
            messages = self.get_messages()
        except (json.JSONDecodeError, UnicodeDecodeError):
            # open(..., "w") truncated the file before json.dump. A crash or a
            # bad read then looked like an empty session and the next add
            # replaced history with one row. Leave an unreadable file alone.
            log.exception("JSONHistory: refusing to overwrite unreadable session %s", self.session_id)
            return
        mutate(messages)
        try:
            self.replace_messages(messages)
        except (OSError, TypeError):
            log.exception(error)
            raise

    def add_message(self, role: str, content: Any, tool_calls: Any = None) -> None:
        msg_dict = message_to_dict(role, content, tool_calls)
        self._modify(lambda msgs: msgs.append(msg_dict), "JSONHistory: Error saving message")

    def update_last_user_content(self, content: Any) -> None:
        msg_dict = message_to_dict("user", content)

        def mutate(messages: list[dict[str, Any]]) -> None:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    messages[i] = msg_dict
                    break
            else:
                messages.append(msg_dict)

        self._modify(mutate, "JSONHistory: Error updating last user message")

    def get_messages(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            if not isinstance(msgs, list) or any(not isinstance(item, dict) for item in msgs):
                # Session file must be a JSON list of objects; other top-level shapes are rejected.
                log.error("JSONHistory: session %s is not a list of objects", self.session_id)
                raise json.JSONDecodeError("session is not a list of objects", "", 0)
            log.debug(f"JSONHistory: Retrieved {len(msgs)} messages for session {self.session_id}")
            return msgs
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # Callers (ChatSession open, add_message) must not treat a corrupt
            # file or OS error as an empty history and write a fresh system row over it.
            log.exception("JSONHistory: Error reading messages")
            raise

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace this session file in one ``os.replace``."""
        atomic_write_text(self.file_path, json.dumps(messages, indent=2), prefix=".history-")


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


def fork_doc_chat_history(old_session_id: str, new_session_id: str) -> None:
    """Copy document-chat rows onto *new_session_id*.

    Snapshot the source first and replace the destination in one write.
    ``clear()`` then ``get_messages()`` wipes the only store when the
    regenerated id is the stored id, and a JSON ``clear`` that swallows
    ``OSError`` then appends onto the rows it failed to remove. Save As
    re-enters setup after a partial property write, or ``os.remove`` fails
    and the copy still runs. The caller skips this when the two ids are
    equal.
    """
    # If the destination already has a conversation, leave it intact.
    # We do not overwrite the destination's chat when "Save As" targets
    # an existing file that already has its own conversation. A seeded
    # system prompt alone (Clear writes one) is not a conversation.
    dest_history = get_chat_history(new_session_id)
    if any(msg.get("role") != "system" for msg in dest_history.get_messages()):
        return

    messages = get_chat_history(old_session_id).get_messages()
    dest_history.replace_messages(messages)

