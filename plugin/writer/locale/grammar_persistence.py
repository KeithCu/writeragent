# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent storage for grammar check results in user-defined document properties.

Per-document persistence stores sentence results in user-defined document properties
and keeps a process-local map keyed by LibreOffice ``aDocumentIdentifier`` (often a
small integer per open doc, not ``RuntimeUID``). ``get_persistence(ctx, doc_id, model=...)``
binds that id to the Writer model on first ``doProofreading``; ``OnUnload`` / dispose
removes map entries so instances can be garbage-collected.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("writeragent.grammar")

from . import grammar_proofread_json
from .grammar_proofread_locale import GRAMMAR_CACHE_VERSION, GRAMMAR_DOC_CACHE_UDPROP



from plugin.framework.uno_listeners import BaseDocumentEventListener

_HAVE_UNO_DOC_EVENTS = False
try:
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl  # noqa: F401
    _HAVE_UNO_DOC_EVENTS = True
except ImportError:
    pass

class GrammarRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.doc_persistence_instances: dict[str, "DocumentPersistence"] = {}
        self.sentence_cache: collections.OrderedDict[str, tuple[str, str, bool, list[dict[str, Any]]]] = collections.OrderedDict()
        self.ignored_rules: set[str] = set()
        self.doc_locales_cache: dict[str, tuple[float, list[str]]] = {}
        self.lang_detect_cache: collections.OrderedDict[str, str] = collections.OrderedDict()

    def get_persistence(self, ctx: Any, doc_id: str | None, *, model: Any = None) -> DocumentPersistence | None:
        if ctx is None or not doc_id:
            return None
        with self.lock:
            existing = self.doc_persistence_instances.get(doc_id)
            if existing is not None:
                if model is not None:
                    existing._bind_model(model)
                return existing
        # Construct outside the registry lock: __init__ may register UNO listeners.
        dp = DocumentPersistence(ctx, doc_id, model=model)
        with self.lock:
            existing = self.doc_persistence_instances.get(doc_id)
            if existing is not None:
                if model is not None:
                    existing._bind_model(model)
                return existing
            self.doc_persistence_instances[doc_id] = dp
            return dp

    def remove_persistence(self, doc_id: str) -> None:
        with self.lock:
            self.doc_persistence_instances.pop(doc_id, None)

    def clear_for_doc(self, doc_id: str) -> None:
        with self.lock:
            self.doc_locales_cache.pop(doc_id, None)
            dp = self.doc_persistence_instances.pop(doc_id, None)
            if dp:
                try:
                    dp._unregister_listeners()
                    with dp._lock:
                        dp._memory_cache.clear()
                        dp._session_accessed.clear()
                except Exception as e:
                    log.debug("[grammar] GrammarRegistry.clear_for_doc failure: %s", e)
                dp._model = None
                dp._teardown_done = True

    def clear_all(self, ctx: Any | None = None) -> None:
        with self.lock:
            self.sentence_cache.clear()
            self.ignored_rules.clear()
            self.doc_locales_cache.clear()
            self.lang_detect_cache.clear()
            snap = list(self.doc_persistence_instances.values())
            self.doc_persistence_instances.clear()
        
        for dp in snap:
            try:
                dp._unregister_listeners()
                with dp._lock:
                    dp._memory_cache.clear()
                    dp._session_accessed.clear()
            except Exception as e:
                log.debug("[grammar] GrammarRegistry.clear_all persistence cleanup failure: %s", e)
            dp._model = None
            dp._teardown_done = True

    def shutdown(self) -> None:
        self.clear_all()

grammar_registry = GrammarRegistry()


def get_document_model_for_id(ctx: Any, doc_id: str) -> Any | None:
    """Return the Writer model for a proofreading document id, if already bound via get_persistence."""
    del ctx
    p = grammar_registry.doc_persistence_instances.get(doc_id)
    if p is not None and p._model is not None:
        return p._model
    return None


class GrammarPersistence(ABC):
    """Abstract base for persistent grammar cache."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._ignored_rules: set[str] = set()
        self._lock = threading.Lock()

    def _persist_to_udprops(self) -> None:
        pass

    @abstractmethod
    def get(self, fp: str) -> list[dict[str, Any]] | None:
        pass

    @abstractmethod
    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        pass

    @abstractmethod
    def clear(self) -> None:
        pass


def _dispatch_doc_event(outer: "DocumentPersistence", event_name: str) -> None:
    """Route XDocumentEventListener.documentEventOccured to the right persistence action.

    Shared between the real UNO listener and the no-UNO stub so a single source
    of truth defines which events trigger save vs teardown.
    """
    if event_name in ("OnPrepareSave", "OnSave", "OnSaveAs", "OnSaveTo"):
        outer._persist_to_udprops()
    elif event_name == "OnUnload":
        outer._teardown()


# XDocumentEventListener extends com.sun.star.lang.XEventListener, so a single
# class handles both document events (incl. OnUnload) and broadcaster disposal.
class _GrammarDocumentEventListener(BaseDocumentEventListener):
    def __init__(self, outer: DocumentPersistence) -> None:
        super().__init__()
        self._outer = outer

    def on_document_event(self, Event: Any) -> None:
        try:
            name = getattr(Event, "EventName", "") or ""
        except Exception:
            return
        _dispatch_doc_event(self._outer, name)

    def on_disposing(self, Source: Any) -> None:
        self._outer._teardown()


class DocumentPersistence(GrammarPersistence):
    """In-memory grammar sentence cache with JSON in user-defined document properties on save."""

    def __init__(self, ctx: Any, doc_id: str, *, model: Any = None) -> None:
        super().__init__(ctx)
        self._doc_id = doc_id
        self._lock = threading.Lock()
        self._memory_cache: dict[str, list[dict[str, Any]]] = {}
        self._session_accessed: set[str] = set()
        self._ignored_rules: set[str] = set()
        self._model: Any = model
        self._doc_listener: Any = None
        self._teardown_done = False
        if self._model:
            self._load_from_udprops()
            self._register_listeners()
        else:
            log.debug("[grammar] DocumentPersistence: no model for doc_id=%s (in-memory only until resolved)", doc_id[:32] if doc_id else "")

    def _bind_model(self, model: Any) -> None:
        """Attach the Writer model after init when ``get_persistence(..., model=...)`` runs."""
        if self._teardown_done or self._model is not None or model is None:
            return
        with self._lock:
            if self._teardown_done or self._model is not None:
                return
            self._model = model
        self._load_from_udprops()
        self._register_listeners()
        log.debug("[grammar] DocumentPersistence: bound model for doc_id=%s (loaded %s cache entries)", self._doc_id[:32] if self._doc_id else "", len(self._memory_cache))

    def _register_listeners(self) -> None:
        if not _HAVE_UNO_DOC_EVENTS or self._model is None:
            return
        if self._doc_listener is not None:
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
        from plugin.doc.udprops import get_document_property

        if not self._model:
            return
        try:
            raw = get_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, None)
            if not raw or not isinstance(raw, str):
                log.debug("[grammar] DocumentPersistence: no cached property on doc_id=%s", self._doc_id[:32] if self._doc_id else "")
                return
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            
            version = data.get("version", 1)
            if version < GRAMMAR_CACHE_VERSION:
                log.debug("[grammar] DocumentPersistence: ignoring old-version cache (v=%s < %s) on doc_id=%s", version, GRAMMAR_CACHE_VERSION, self._doc_id[:32] if self._doc_id else "")
                return

            with self._lock:
                self._memory_cache = {}
                # Good sentences (no errors)
                good = data.get("good")
                if isinstance(good, list):
                    for fp in good:
                        self._memory_cache[str(fp)] = []
                
                # Bad sentences (with errors)
                bad = data.get("bad")
                if isinstance(bad, dict):
                    for fp, compressed_errors in bad.items():
                        if isinstance(compressed_errors, list):
                             self._memory_cache[str(fp)] = [grammar_proofread_json.decompress_error(e) for e in compressed_errors if isinstance(e, dict)]
                
                # Ignored rules
                self._ignored_rules = set(data.get("ignored_rules", []))
                
                loaded_count = len(self._memory_cache)
            log.debug("[grammar] DocumentPersistence: loaded %s sentences from udprop (doc_id=%s, v=%s)", loaded_count, self._doc_id[:32] if self._doc_id else "", version)
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: load user property failed: %s", e)

    def _persist_to_udprops(self) -> None:
        from plugin.doc.udprops import set_document_property

        if not self._model:
            return
        try:
            with self._lock:
                good_fps = []
                bad_map = {}
                for fp in self._session_accessed:
                    if fp in self._memory_cache:
                        errs = self._memory_cache[fp]
                        if not errs:
                            good_fps.append(fp)
                        else:
                             bad_map[fp] = [grammar_proofread_json.compress_error(e) for e in errs]
                ignored_rules_list = list(self._ignored_rules)
            
            payload_dict = {
                "version": GRAMMAR_CACHE_VERSION,
                "good": good_fps,
                "bad": bad_map,
                "ignored_rules": ignored_rules_list,
            }
            payload = json.dumps(payload_dict)
            if len(payload) > 900_000:
                log.warning("[grammar] DocumentPersistence: cache JSON too large (%s bytes), skip write", len(payload))
                return
            set_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, payload)
            log.debug("[grammar] DocumentPersistence: saved %s sentences (%s bytes) to udprop (doc_id=%s, v=%s)", len(good_fps) + len(bad_map), len(payload), self._doc_id[:32] if self._doc_id else "", GRAMMAR_CACHE_VERSION)
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
        grammar_registry.remove_persistence(self._doc_id)
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

    def clear(self) -> None:
        with self._lock:
            self._memory_cache.clear()
            self._session_accessed.clear()


def get_persistence(ctx: Any, doc_id: str | None = None, *, model: Any = None) -> GrammarPersistence | None:
    """Return per-document persistence for grammar sentence cache."""
    return grammar_registry.get_persistence(ctx, doc_id, model=model)


def clear_all_document_persistence(ctx: Any) -> None:
    """Remove every ``DocumentPersistence`` (listeners + map); for tests / reset without doc_id."""
    grammar_registry.clear_all(ctx)

_doc_persistence_instances = grammar_registry.doc_persistence_instances
