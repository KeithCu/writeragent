import json
import sqlite3
import logging
import os

logger = logging.getLogger(__name__)

try:
    from sqlalchemy import create_engine, Column, Integer, Text, select, delete
    from sqlalchemy.orm import sessionmaker, declarative_base, Session
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

from plugin.framework.config import user_config_dir
from plugin.framework.uno_context import get_ctx

def _get_db_path():
    ctx = get_ctx()
    config_dir = user_config_dir(ctx)
    if config_dir:
        try:
            if not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)
        except Exception as e:
            from plugin.framework.logging import debug_log
            debug_log(f"Error creating config directory: {e}", context="HistoryDB")
        path = os.path.join(config_dir, "localwriter_history.db")
        from plugin.framework.logging import debug_log
        debug_log(f"Using database path: {path}", context="HistoryDB")
        return path
    return "localwriter_history.db"

# LangChain-compatible JSON conversion
def message_to_dict(role, content, tool_calls=None):
    return {
        "role": role,
        "content": content,
        "tool_calls": tool_calls
    }

# ---------------------------------------------------------------------------
# SQLAlchemy Implementation (Mimics LangChain)
# ---------------------------------------------------------------------------
if HAS_SQLALCHEMY:
    Base = declarative_base()

    class MessageRecord(Base):
        __tablename__ = "message_store"
        id = Column(Integer, primary_key=True)
        session_id = Column(Text, index=True)
        message = Column(Text)

    class SQLAlchemyHistory:
        def __init__(self, session_id, db_path):
            self.session_id = session_id
            self.engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)

        def add_message(self, role, content, tool_calls=None):
            msg_dict = message_to_dict(role, content, tool_calls)
            with self.SessionLocal() as session:
                record = MessageRecord(
                    session_id=self.session_id,
                    message=json.dumps(msg_dict)
                )
                session.add(record)
                session.commit()
                from plugin.framework.logging import debug_log
                debug_log(f"SQLAlchemy: Added message for session {self.session_id}", context="HistoryDB")

        def get_messages(self):
            with self.SessionLocal() as session:
                records = session.query(MessageRecord).filter(
                    MessageRecord.session_id == self.session_id
                ).order_by(MessageRecord.id.asc()).all()
                msgs = [json.loads(r.message) for r in records]
                from plugin.framework.logging import debug_log
                debug_log(f"SQLAlchemy: Retreived {len(msgs)} messages for session {self.session_id}", context="HistoryDB")
                return msgs

        def clear(self):
            with self.SessionLocal() as session:
                session.query(MessageRecord).filter(
                    MessageRecord.session_id == self.session_id
                ).delete()
                session.commit()

# ---------------------------------------------------------------------------
# Native SQLite3 Implementation (Fallback)
# ---------------------------------------------------------------------------
class SQLite3History:
    def __init__(self, session_id, db_path):
        self.session_id = session_id
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
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

    def add_message(self, role, content, tool_calls=None):
        msg_dict = message_to_dict(role, content, tool_calls)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO message_store (session_id, message) VALUES (?, ?)",
                (self.session_id, json.dumps(msg_dict))
            )
            conn.commit()
            from plugin.framework.logging import debug_log
            debug_log(f"SQLite3: Added message for session {self.session_id}", context="HistoryDB")

    def get_messages(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT message FROM message_store WHERE session_id = ? ORDER BY id ASC",
                (self.session_id,)
            )
            msgs = [json.loads(row[0]) for row in cursor.fetchall()]
            from plugin.framework.logging import debug_log
            debug_log(f"SQLite3: Retreived {len(msgs)} messages for session {self.session_id}", context="HistoryDB")
            return msgs

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM message_store WHERE session_id = ?", (self.session_id,))
            conn.commit()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_chat_history(session_id, db_path=None):
    if not db_path:
        db_path = _get_db_path()
    if HAS_SQLALCHEMY:
        logger.debug(f"Using SQLAlchemy for chat history at {db_path}")
        return SQLAlchemyHistory(session_id, db_path)
    else:
        logger.debug(f"Using native sqlite3 for chat history at {db_path}")
        return SQLite3History(session_id, db_path)
