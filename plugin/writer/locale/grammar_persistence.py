# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent storage for grammar check results (SQLite with JSON fallback).

When ``USE_SQLITE_CACHE`` is False, per-document persistence stores sentence results in
user-defined document properties and keeps a process-local map keyed by ``RuntimeUID``
(see ``aDocumentIdentifier`` in the proofreader); ``OnUnload`` / dispose removes map
entries so instances can be garbage-collected.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("writeragent.grammar")

# When True (default): global SQLite or JSON-under-profile cache (existing behavior).
# When False: ``get_persistence(ctx, doc_id)`` returns ``DocumentPersistence`` keyed by
# document id; sentence cache layer bypasses the global LRU (see ``grammar_proofread_cache``).
USE_SQLITE_CACHE = True

GRAMMAR_DOC_CACHE_UDPROP = "WriterAgentGrammarCache"

try:
    import sqlite3

    HAS_SQLITE = True
except ImportError:
    sqlite3 = None  # type: ignore
    HAS_SQLITE = False

_unohelper: Any = None
_XDocumentEventListener: Any = None
_HAVE_UNO_DOC_EVENTS = False
try:
    import unohelper as _unohelper_impl
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl

    _unohelper = _unohelper_impl
    _XDocumentEventListener = _XDocumentEventListener_impl
    _HAVE_UNO_DOC_EVENTS = True
except ImportError:
    pass

CACHE_LIMIT = 5000
PRUNE_TARGET = 4000

_persistence_instance: GrammarPersistence | None = None
_persistence_init_lock = threading.Lock()

_doc_persistence_instances: dict[str, "DocumentPersistence"] = {}
_doc_map_lock = threading.Lock()


_DESKTOP_ITER_HARD_CAP = 1024


def _iter_desktop_components(ctx: Any) -> Any:
    from plugin.framework.uno_context import get_desktop

    # Skip enumeration entirely when UNO isn't actually loaded. Without
    # _HAVE_UNO_DOC_EVENTS we cannot register listeners anyway (DocumentPersistence
    # stays in-memory only), and walking a non-UNO ``ctx`` (e.g. a MagicMock in
    # pytest) can infinite-loop because Mock.hasMoreElements() is always truthy.
    if not _HAVE_UNO_DOC_EVENTS:
        return
    try:
        desktop = get_desktop(ctx)
        comps = desktop.getComponents()
        if not comps:
            return
        enum = comps.createEnumeration()
        if not enum:
            return
        # Belt-and-suspenders cap: real desktops have a small number of open docs;
        # a high finite cap prevents runaway iteration if a future caller passes a
        # non-UNO enumeration stub.
        for _ in range(_DESKTOP_ITER_HARD_CAP):
            if not enum.hasMoreElements():
                return
            yield enum.nextElement()
        log.warning("[grammar] _iter_desktop_components: hit hard cap %s", _DESKTOP_ITER_HARD_CAP)
    except Exception as e:
        log.debug("[grammar] enumerate desktop components: %s", e)


def _model_runtime_uid(model: Any) -> str | None:
    try:
        if hasattr(model, "getPropertyValue") and hasattr(model, "getPropertySetInfo"):
            info = model.getPropertySetInfo()
            if info is not None and info.hasPropertyByName("RuntimeUID"):
                v = model.getPropertyValue("RuntimeUID")
                if v is not None:
                    return str(v)
    except Exception as e:
        log.debug("[grammar] RuntimeUID read failed: %s", e)
    return None


def _find_model_by_runtime_uid(ctx: Any, doc_id: str) -> Any | None:
    for comp in _iter_desktop_components(ctx):
        try:
            uid = _model_runtime_uid(comp)
            if uid and uid == doc_id:
                return comp
        except Exception:
            continue
    return None


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
    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
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
                        errors_json TEXT,
                        last_used INTEGER
                    )
                """)
                self._migrate_drop_text_column(conn)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_last_used ON sentence_cache(last_used)")
                conn.commit()
        except Exception as e:
            log.error("[grammar] SQLitePersistence _init_db failed: %s", e)

    def _migrate_drop_text_column(self, conn: Any) -> None:
        try:
            cols = [str(row[1]) for row in conn.execute("PRAGMA table_info(sentence_cache)").fetchall()]
            if "text" not in cols:
                return
            # The sentence text was never read after writes; keep only the stable
            # fingerprint and errors to reduce cache size and plaintext footprint.
            conn.executescript("""
                DROP INDEX IF EXISTS idx_last_used;
                CREATE TABLE sentence_cache_new (
                    fingerprint TEXT PRIMARY KEY,
                    locale TEXT,
                    errors_json TEXT,
                    last_used INTEGER
                );
                INSERT INTO sentence_cache_new (fingerprint, locale, errors_json, last_used)
                    SELECT fingerprint, locale, errors_json, last_used FROM sentence_cache;
                DROP TABLE sentence_cache;
                ALTER TABLE sentence_cache_new RENAME TO sentence_cache;
            """)
            log.info("[grammar] SQLitePersistence: migrated sentence_cache schema without text column")
        except Exception as e:
            log.warning("[grammar] SQLitePersistence text-column migration failed: %s", e)

    def get(self, fp: str) -> list[dict[str, Any]] | None:
        if not HAS_SQLITE or sqlite3 is None:
            return None
        try:
            with sqlite3.connect(self.base_path) as conn:
                cursor = conn.execute("SELECT errors_json FROM sentence_cache WHERE fingerprint = ?", (fp,))
                row = cursor.fetchone()
                if row:
                    conn.execute("UPDATE sentence_cache SET last_used = ? WHERE fingerprint = ?", (int(time.time()), fp))
                    conn.commit()
                    return json.loads(row[0])
        except Exception as e:
            log.debug("[grammar] SQLitePersistence get failed: %s", e)
        return None

    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        if not HAS_SQLITE or sqlite3 is None:
            return
        try:
            errors_json = json.dumps(errors)
            with sqlite3.connect(self.base_path) as conn:
                conn.execute("""
                    INSERT INTO sentence_cache (fingerprint, locale, errors_json, last_used)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        errors_json = excluded.errors_json,
                        last_used = excluded.last_used
                """, (fp, locale, errors_json, int(time.time())))
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
                os.utime(path, None)
                return data.get("errors")
        except Exception as e:
            log.debug("[grammar] JSONPersistence get failed: %s", e)
        return None

    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        path = self._file_path(fp)
        try:
            data = {
                "fingerprint": fp,
                "locale": locale,
                "errors": errors,
                "timestamp": int(time.time()),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning("[grammar] JSONPersistence put failed: %s", e)

    def prune(self) -> None:
        try:
            files = [os.path.join(self.base_path, f) for f in os.listdir(self.base_path) if f.endswith(".json")]
            if len(files) > CACHE_LIMIT:
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


def _dispatch_doc_event(outer: "DocumentPersistence", event_name: str) -> None:
    """Route XDocumentEventListener.documentEventOccured to the right persistence action.

    Shared between the real UNO listener and the no-UNO stub so a single source
    of truth defines which events trigger save vs teardown.
    """
    if event_name in ("OnPrepareSave", "OnSave", "OnSaveAs", "OnSaveTo"):
        outer._persist_to_udprops()
    elif event_name == "OnUnload":
        outer._teardown()


if _HAVE_UNO_DOC_EVENTS:
    assert _unohelper is not None
    assert _XDocumentEventListener is not None

    # XDocumentEventListener extends com.sun.star.lang.XEventListener, so a single
    # class handles both document events (incl. OnUnload) and broadcaster disposal.
    # The UNO interface name is `documentEventOccured`; defining `documentEvent`
    # would silently no-op on save events.
    class _GrammarDocumentEventListener(_unohelper.Base, _XDocumentEventListener):  # type: ignore[misc, valid-type]
        def __init__(self, outer: DocumentPersistence) -> None:
            super().__init__()
            self._outer = outer

        def documentEventOccured(self, Event: Any) -> None:  # noqa: N802, N803  -- UNO IDL signature
            try:
                name = getattr(Event, "EventName", "") or ""
            except Exception:
                return
            _dispatch_doc_event(self._outer, name)

        def disposing(self, Source: Any) -> None:  # noqa: N803  -- UNO IDL signature
            self._outer._teardown()

else:

    # Test/no-UNO stub: mirrors the real class signature so unit tests can exercise
    # the event dispatch logic (especially the documentEventOccured method name,
    # which was previously typoed as ``documentEvent`` and silently dropped saves).
    class _GrammarDocumentEventListener:  # type: ignore[no-redef]
        def __init__(self, outer: Any) -> None:
            self._outer = outer

        def documentEventOccured(self, Event: Any) -> None:  # noqa: N802, N803  -- UNO IDL signature
            try:
                name = getattr(Event, "EventName", "") or ""
            except Exception:
                return
            _dispatch_doc_event(self._outer, name)

        def disposing(self, Source: Any) -> None:  # noqa: N803  -- UNO IDL signature
            self._outer._teardown()


class DocumentPersistence(GrammarPersistence):
    """In-memory grammar sentence cache with JSON in user-defined document properties on save."""

    def __init__(self, ctx: Any, doc_id: str) -> None:
        super().__init__(ctx, "")
        self._doc_id = doc_id
        self._lock = threading.Lock()
        self._memory_cache: dict[str, list[dict[str, Any]]] = {}
        self._session_accessed: set[str] = set()
        self._model: Any = _find_model_by_runtime_uid(ctx, doc_id)
        self._doc_listener: Any = None
        self._teardown_done = False
        if self._model:
            self._load_from_udprops()
            self._register_listeners()
        else:
            log.debug("[grammar] DocumentPersistence: no model for doc_id=%s (in-memory only until resolved)", doc_id[:32] if doc_id else "")

    def _register_listeners(self) -> None:
        if not _HAVE_UNO_DOC_EVENTS or self._model is None:
            return
        # XDocumentEventListener handles both OnSave/OnUnload (via documentEventOccured)
        # and broadcaster teardown (via disposing inherited from lang.XEventListener),
        # so a single registration on XDocumentEventBroadcaster covers both paths.
        try:
            self._doc_listener = _GrammarDocumentEventListener(self)
            if hasattr(self._model, "addDocumentEventListener"):
                self._model.addDocumentEventListener(self._doc_listener)
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: listener registration failed: %s", e)

    def _unregister_listeners(self) -> None:
        m = self._model
        if m is None:
            return
        try:
            if self._doc_listener is not None and hasattr(m, "removeDocumentEventListener"):
                m.removeDocumentEventListener(self._doc_listener)
        except Exception as e:
            log.debug("[grammar] removeDocumentEventListener: %s", e)
        self._doc_listener = None

    def _load_from_udprops(self) -> None:
        from plugin.doc.document_helpers import get_document_property

        if not self._model:
            return
        try:
            raw = get_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, None)
            if not raw or not isinstance(raw, str):
                log.debug("[grammar] DocumentPersistence: no cached property on doc_id=%s", self._doc_id[:32] if self._doc_id else "")
                return
            data = json.loads(raw)
            if isinstance(data, dict):
                with self._lock:
                    self._memory_cache = {}
                    for k, v in data.items():
                        if isinstance(v, list):
                            self._memory_cache[str(k)] = [dict(e) for e in v if isinstance(e, dict)]
                    loaded_count = len(self._memory_cache)
                log.debug("[grammar] DocumentPersistence: loaded %s sentences from udprop (doc_id=%s)", loaded_count, self._doc_id[:32] if self._doc_id else "")
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: load user property failed: %s", e)

    def _persist_to_udprops(self) -> None:
        from plugin.doc.document_helpers import set_document_property

        if not self._model:
            return
        try:
            with self._lock:
                pruned = {k: self._memory_cache[k] for k in self._session_accessed if k in self._memory_cache}
            payload = json.dumps(pruned)
            if len(payload) > 900_000:
                log.warning("[grammar] DocumentPersistence: cache JSON too large (%s bytes), skip write", len(payload))
                return
            set_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, payload)
            log.debug("[grammar] DocumentPersistence: saved %s sentences (%s bytes) to udprop (doc_id=%s)", len(pruned), len(payload), self._doc_id[:32] if self._doc_id else "")
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: save user property failed: %s", e)

    def _teardown(self) -> None:
        if self._teardown_done:
            return
        self._teardown_done = True
        self._unregister_listeners()
        with self._lock:
            self._memory_cache.clear()
            self._session_accessed.clear()
        with _doc_map_lock:
            _doc_persistence_instances.pop(self._doc_id, None)
        self._model = None

    def get(self, fp: str) -> list[dict[str, Any]] | None:
        with self._lock:
            self._session_accessed.add(fp)
            hit = self._memory_cache.get(fp)
            return list(hit) if hit is not None else None

    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        with self._lock:
            self._session_accessed.add(fp)
            self._memory_cache[fp] = [dict(e) for e in errors]

    def prune(self) -> None:
        pass

    def clear(self) -> None:
        with self._lock:
            self._memory_cache.clear()
            self._session_accessed.clear()


def _get_sqlite_singleton(ctx: Any) -> GrammarPersistence | None:
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


def get_persistence(ctx: Any, doc_id: str | None = None) -> GrammarPersistence | None:
    """Return persistence: global SQLite/JSON when ``USE_SQLITE_CACHE`` else per-document."""
    if USE_SQLITE_CACHE:
        return _get_sqlite_singleton(ctx)
    if ctx is None or not doc_id:
        return None
    with _doc_map_lock:
        existing = _doc_persistence_instances.get(doc_id)
        if existing is not None:
            return existing
        dp = DocumentPersistence(ctx, doc_id)
        _doc_persistence_instances[doc_id] = dp
        return dp


def clear_all_document_persistence(ctx: Any) -> None:
    """Remove every ``DocumentPersistence`` (listeners + map); for tests / reset without doc_id."""
    if USE_SQLITE_CACHE:
        return
    with _doc_map_lock:
        snap = list(_doc_persistence_instances.values())
        _doc_persistence_instances.clear()
    for dp in snap:
        try:
            dp._unregister_listeners()
            with dp._lock:
                dp._memory_cache.clear()
                dp._session_accessed.clear()
        except Exception as e:
            log.debug("[grammar] clear_all_document_persistence item: %s", e)
        dp._model = None
        dp._teardown_done = True
