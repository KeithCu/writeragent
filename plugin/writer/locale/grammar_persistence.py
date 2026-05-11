# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent storage for grammar check results (SQLite with JSON fallback)."""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any


log = logging.getLogger("writeragent.grammar")

try:
    import sqlite3
    HAS_SQLITE = True
except ImportError:
    sqlite3 = None  # type: ignore
    HAS_SQLITE = False

CACHE_LIMIT = 5000
PRUNE_TARGET = 4000


class GrammarPersistence(ABC):
    """Abstract base for persistent grammar cache."""

    def __init__(self, ctx: Any, base_path: str):
        self.ctx = ctx
        self.base_path = base_path
        self._pruned = False

    @abstractmethod
    def get(self, fp: str) -> list[dict[str, Any]] | None:
        pass

    @abstractmethod
    def put(self, fp: str, locale: str, text: str, errors: list[dict[str, Any]]) -> None:
        pass

    @abstractmethod
    def prune(self) -> None:
        pass

    @abstractmethod
    def clear(self) -> None:
        pass

    def ensure_pruned(self) -> None:
        """Run prune exactly once per session/initialization."""
        if self._pruned:
            return
        self._pruned = True
        try:
            self.prune()
        except Exception as e:
            log.warning("[grammar] persistence prune failed: %s", e)


class SQLitePersistence(GrammarPersistence):
    """SQLite implementation of persistent grammar cache."""

    def __init__(self, ctx: Any, db_path: str):
        super().__init__(ctx, db_path)
        self._init_db()

    def _init_db(self) -> None:
        if not HAS_SQLITE or sqlite3 is None:
            return
        try:
            os.makedirs(os.path.dirname(self.base_path), exist_ok=True)
            with sqlite3.connect(self.base_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sentence_cache (
                        fingerprint TEXT PRIMARY KEY,
                        locale TEXT,
                        text TEXT,
                        errors_json TEXT,
                        last_used INTEGER
                    )
                """)
                # Index for pruning and locale lookups
                conn.execute("CREATE INDEX IF NOT EXISTS idx_last_used ON sentence_cache(last_used)")
                conn.commit()
        except Exception as e:
            log.error("[grammar] SQLitePersistence _init_db failed: %s", e)

    def get(self, fp: str) -> list[dict[str, Any]] | None:
        if not HAS_SQLITE or sqlite3 is None:
            return None
        try:
            with sqlite3.connect(self.base_path) as conn:
                cursor = conn.execute("SELECT errors_json FROM sentence_cache WHERE fingerprint = ?", (fp,))
                row = cursor.fetchone()
                if row:
                    # Update last_used (silent best-effort)
                    conn.execute("UPDATE sentence_cache SET last_used = ? WHERE fingerprint = ?", (int(time.time()), fp))
                    conn.commit()
                    return json.loads(row[0])
        except Exception as e:
            log.debug("[grammar] SQLitePersistence get failed: %s", e)
        return None

    def put(self, fp: str, locale: str, text: str, errors: list[dict[str, Any]]) -> None:
        if not HAS_SQLITE or sqlite3 is None:
            return
        try:
            errors_json = json.dumps(errors)
            with sqlite3.connect(self.base_path) as conn:
                conn.execute("""
                    INSERT INTO sentence_cache (fingerprint, locale, text, errors_json, last_used)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        errors_json = excluded.errors_json,
                        last_used = excluded.last_used
                """, (fp, locale, text, errors_json, int(time.time())))
                conn.commit()
        except Exception as e:
            log.warning("[grammar] SQLitePersistence put failed: %s", e)

    def prune(self) -> None:
        if not HAS_SQLITE or sqlite3 is None:
            return
        try:
            with sqlite3.connect(self.base_path) as conn:
                cursor = conn.execute("SELECT count(*) FROM sentence_cache")
                count = cursor.fetchone()[0]
                if count > CACHE_LIMIT:
                    to_remove = count - PRUNE_TARGET
                    log.info("[grammar] persistence: pruning %s entries from SQLite cache", to_remove)
                    conn.execute("""
                        DELETE FROM sentence_cache WHERE fingerprint IN (
                            SELECT fingerprint FROM sentence_cache ORDER BY last_used ASC LIMIT ?
                        )
                    """, (to_remove,))
                    conn.commit()
        except Exception as e:
            log.warning("[grammar] SQLitePersistence prune failed: %s", e)

    def clear(self) -> None:
        if not HAS_SQLITE or sqlite3 is None:
            return
        try:
            with sqlite3.connect(self.base_path) as conn:
                conn.execute("DELETE FROM sentence_cache")
                conn.commit()
        except Exception as e:
            log.warning("[grammar] SQLitePersistence clear failed: %s", e)


class JSONPersistence(GrammarPersistence):
    """JSON-sharded implementation of persistent grammar cache (fallback)."""

    def __init__(self, ctx: Any, dir_path: str):
        # base_path is the directory containing .json files
        super().__init__(ctx, dir_path)
        try:
            os.makedirs(self.base_path, exist_ok=True)
        except Exception as e:
            log.error("[grammar] JSONPersistence init failed to create dir: %s", e)

    def _file_path(self, fp: str) -> str:
        return os.path.join(self.base_path, f"{fp}.json")

    def get(self, fp: str) -> list[dict[str, Any]] | None:
        path = self._file_path(fp)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Update mtime for pruning
                os.utime(path, None)
                return data.get("errors")
        except Exception as e:
            log.debug("[grammar] JSONPersistence get failed: %s", e)
        return None

    def put(self, fp: str, locale: str, text: str, errors: list[dict[str, Any]]) -> None:
        path = self._file_path(fp)
        try:
            data = {
                "fingerprint": fp,
                "locale": locale,
                "text": text,
                "errors": errors,
                "timestamp": int(time.time())
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning("[grammar] JSONPersistence put failed: %s", e)

    def prune(self) -> None:
        try:
            files = [os.path.join(self.base_path, f) for f in os.listdir(self.base_path) if f.endswith(".json")]
            if len(files) > CACHE_LIMIT:
                # Sort by mtime
                files.sort(key=os.path.getmtime)
                to_remove = len(files) - PRUNE_TARGET
                log.info("[grammar] persistence: pruning %s files from JSON cache", to_remove)
                for i in range(to_remove):
                    try:
                        os.remove(files[i])
                    except OSError:
                        pass
        except Exception as e:
            log.warning("[grammar] JSONPersistence prune failed: %s", e)

    def clear(self) -> None:
        try:
            for f in os.listdir(self.base_path):
                if f.endswith(".json"):
                    try:
                        os.remove(os.path.join(self.base_path, f))
                    except OSError:
                        pass
        except Exception as e:
            log.warning("[grammar] JSONPersistence clear failed: %s", e)


_persistence_instance: GrammarPersistence | None = None
import threading
_persistence_init_lock = threading.Lock()

def get_persistence(ctx: Any) -> GrammarPersistence | None:
    """Return (and initialize once) the best available persistence implementation."""
    global _persistence_instance
    if _persistence_instance is not None:
        return _persistence_instance

    if ctx is None:
        return None

    with _persistence_init_lock:
        if _persistence_instance is not None:
            return _persistence_instance

        from plugin.framework.config import user_config_dir
        try:
            config_dir = user_config_dir(ctx)
            if not config_dir:
                return None
            
            if HAS_SQLITE:
                db_path = os.path.join(config_dir, "writeragent_grammar.db")
                _persistence_instance = SQLitePersistence(ctx, db_path)
            else:
                dir_path = os.path.join(config_dir, "writeragent_grammar_cache.d")
                _persistence_instance = JSONPersistence(ctx, dir_path)
            
            _persistence_instance.ensure_pruned()
            return _persistence_instance
        except Exception as e:
            log.warning("[grammar] get_persistence failed: %s", e)
            return None
