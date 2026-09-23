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
import time
import weakref
from typing import Any

log = logging.getLogger("writeragent.grammar")

from plugin.framework.config import grammar_checker_identity

from . import grammar_proofread_json
from .grammar_proofread_locale import GRAMMAR_CACHE_VERSION, GRAMMAR_DOC_CACHE_UDPROP

# v2 blobs with LLM-style errors have no model id. First `llm:…` identity to
# touch the document adopts the rows (young-codebase hack; avoids a full recheck).
_V2_LLM_PENDING = "llm"

_SNIFF_PREFIXES: tuple[tuple[str, str], ...] = (
    ("harper||", "harper"),
    ("languagetool||", "languagetool"),
    ("vale||", "vale"),
)



from plugin.framework.uno_listeners import BaseDocumentEventListener


def sniff_v2_cache_identity(data: dict[str, Any]) -> str | None:
    """Return a checker class for a v2 udprop blob, or None if it must be dropped.

    Local engines are identified from ``r`` prefixes on *bad* rows. LLM-style
    (``wa_g_rule||`` or unprefixed) errors return ``llm`` (pending adopt).
    Good-only blobs and mixed prefixes return None.
    """
    bad = data.get("bad")
    if not isinstance(bad, dict) or not bad:
        return None
    found: set[str] = set()
    for compressed_errors in bad.values():
        if not isinstance(compressed_errors, list):
            continue
        for err in compressed_errors:
            if not isinstance(err, dict):
                continue
            rule = str(err.get("r") or err.get("rule_identifier") or "")
            matched = False
            for prefix, ident in _SNIFF_PREFIXES:
                if rule.startswith(prefix):
                    found.add(ident)
                    matched = True
                    break
            if not matched:
                found.add(_V2_LLM_PENDING)
    if len(found) != 1:
        return None
    return next(iter(found))

_HAVE_UNO_DOC_EVENTS = False
try:
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl  # noqa: F401  # pyright: ignore[reportUnusedImport]
    _HAVE_UNO_DOC_EVENTS = True
except ImportError:
    pass

class GrammarRegistry:
    lock: threading.RLock

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.doc_persistence_instances: dict[str, "DocumentPersistence"] = {}
        self.sentence_cache: collections.OrderedDict[str, tuple[str, str, bool, list[dict[str, Any]]]] = collections.OrderedDict()
        self.ignored_rules: set[str] = set()
        self.doc_locales_cache: dict[str, tuple[float, list[str]]] = {}
        self.lang_detect_cache: collections.OrderedDict[str, str] = collections.OrderedDict()
        self.live_proofreaders: weakref.WeakSet[Any] = weakref.WeakSet()

    def register_live_proofreader(self, proofreader: Any) -> None:
        """Keep the Linguistic-owned proofreader so Settings Recheck can fire PROOFREAD_AGAIN."""
        try:
            self.live_proofreaders.add(proofreader)
        except TypeError:
            log.debug("[grammar] live proofreader is not weak-referenceable")

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
                dp._teardown()
            except Exception as e:
                log.debug("[grammar] GrammarRegistry.clear_for_doc failure: %s", e)

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
                dp._teardown()
            except Exception as e:
                log.debug("[grammar] GrammarRegistry.clear_all persistence cleanup failure: %s", e)

    def get_cached_language(self, text: str) -> str | None:
        with self.lock:
            if text in self.lang_detect_cache:
                self.lang_detect_cache.move_to_end(text)
                return self.lang_detect_cache[text]
        return None

    def put_cached_language(self, text: str, lang: str) -> None:
        with self.lock:
            self.lang_detect_cache[text] = lang
            if len(self.lang_detect_cache) > 1000:
                self.lang_detect_cache.popitem(last=False)

    def shutdown(self) -> None:
        self.clear_all()

grammar_registry = GrammarRegistry()





def get_document_model_for_id(ctx: Any, doc_id: str) -> Any | None:
    """Return the Writer model for a proofreading document id, if already bound via get_persistence.

    ``ctx`` is unused; kept so callers can pass the UNO context they already hold.
    """
    del ctx
    with grammar_registry.lock:
        p = grammar_registry.doc_persistence_instances.get(doc_id)
        if p is not None and p._model is not None:
            return p._model
    return None


# XDocumentEventListener extends com.sun.star.lang.XEventListener, so a single
# class handles both document events (incl. OnUnload) and broadcaster disposal.
class _GrammarDocumentEventListener(BaseDocumentEventListener):
    _outer: DocumentPersistence

    def __init__(self, outer: DocumentPersistence) -> None:
        super().__init__()
        self._outer = outer

    def on_document_event(self, Event: Any) -> None:
        try:
            name = getattr(Event, "EventName", "") or ""
        except Exception:
            return
        if name in ("OnPrepareSave", "OnSave", "OnSaveAs", "OnSaveTo"):
            self._outer._persist_to_udprops()
        elif name == "OnUnload":
            self._outer._teardown()

    def on_disposing(self, Source: Any) -> None:
        self._outer._teardown()


class DocumentPersistence:
    """In-memory grammar sentence persistence backing the unified sentence cache with ODT udprops on save."""

    ctx: Any
    _lock: threading.Lock
    _doc_id: str
    _teardown_done: bool

    def __init__(self, ctx: Any, doc_id: str, *, model: Any = None) -> None:
        self.ctx = ctx
        self._session_accessed: set[str] = set()
        self._ignored_rules: set[str] = set()
        self._lock = threading.Lock()
        self._doc_id = doc_id
        self._entries: dict[str, list[dict[str, Any]]] = {}
        self._blob_identity: str | None = None
        self._session_identity: str | None = None
        self._model: Any = model
        self._doc_listener: Any = None
        self._teardown_done = False
        if self._model:
            self._load_from_udprops()
            self._register_listeners()
        else:
            log.debug("[grammar] DocumentPersistence: no model for doc_id=%s (in-memory only until resolved)", doc_id[:32] if doc_id else "")

    def mark_accessed(self, fp: str) -> None:
        """Record that this fingerprint was used this session (udprop save filter)."""
        with self._lock:
            self._session_accessed.add(fp)

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
        log.debug("[grammar] DocumentPersistence: bound model for doc_id=%s", self._doc_id[:32] if self._doc_id else "")

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

    def _fill_entries_from_payload(self, data: dict[str, Any]) -> int:
        """Populate ``_entries`` from good/bad maps. Caller holds ``_lock``."""
        loaded_count = 0
        good = data.get("good")
        if isinstance(good, list):
            for fp in good:
                self._entries[str(fp)] = []
                loaded_count += 1
        bad = data.get("bad")
        if isinstance(bad, dict):
            for fp, compressed_errors in bad.items():
                if not isinstance(compressed_errors, list):
                    continue
                errs = [
                    grammar_proofread_json.decompress_error(e)
                    for e in compressed_errors
                    if isinstance(e, dict)
                ]
                self._entries[str(fp)] = errs
                loaded_count += 1
        return loaded_count

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
            current = grammar_checker_identity()
            load_entries = False
            blob_identity: str | None = None

            if version >= 3:
                blob_identity = str(data.get("model") or "") or None
                load_entries = bool(blob_identity) and blob_identity == current
            elif version == 2:
                sniffed = sniff_v2_cache_identity(data)
                if sniffed == _V2_LLM_PENDING:
                    blob_identity = _V2_LLM_PENDING
                    load_entries = True
                elif sniffed is not None:
                    blob_identity = sniffed
                    load_entries = sniffed == current
                else:
                    log.debug(
                        "[grammar] DocumentPersistence: dropping unclassifiable v2 cache on doc_id=%s",
                        self._doc_id[:32] if self._doc_id else "",
                    )
            else:
                log.debug(
                    "[grammar] DocumentPersistence: ignoring old-version cache (v=%s) on doc_id=%s",
                    version,
                    self._doc_id[:32] if self._doc_id else "",
                )
                return

            loaded_count = 0
            with self._lock:
                self._ignored_rules = set(data.get("ignored_rules") or [])
                self._blob_identity = blob_identity
                if load_entries:
                    loaded_count = self._fill_entries_from_payload(data)
                else:
                    self._entries.clear()

            log.debug(
                "[grammar] DocumentPersistence: loaded %s sentences from udprop (doc_id=%s, v=%s, blob=%s, current=%s)",
                loaded_count,
                self._doc_id[:32] if self._doc_id else "",
                version,
                blob_identity,
                current,
            )
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: load user property failed: %s", e)

    def ensure_identity(self, identity: str) -> str | None:
        """Bind this doc's L2 to *identity*. Return the previous identity after a switch."""
        with self._lock:
            previous = self._session_identity
            blob = self._blob_identity

            if blob == _V2_LLM_PENDING and identity.startswith("llm:"):
                self._blob_identity = identity
                self._session_identity = identity
                return None

            if previous is None:
                self._session_identity = identity
                if blob and blob != _V2_LLM_PENDING and blob != identity:
                    self._entries.clear()
                    self._session_accessed.clear()
                    self._blob_identity = identity
                elif not blob:
                    self._blob_identity = identity
                return None

            if previous == identity:
                return None

            self._entries.clear()
            self._session_accessed.clear()
            self._session_identity = identity
            self._blob_identity = identity
            return previous

    def _persist_to_udprops(self) -> None:
        # Note (Backlog P22): _persist_to_udprops only serializes _session_accessed keys.
        # LibreOffice calls proofreading for the entire document on open, which populates
        # _session_accessed for active sentences and naturally trims old/deleted sentences
        # from the persisted user-defined property payload.
        from plugin.doc.udprops import set_document_property

        if not self._model:
            return
        try:
            with self._lock:
                if self._blob_identity == _V2_LLM_PENDING:
                    log.debug(
                        "[grammar] DocumentPersistence: skip save; v2 LLM blob not yet adopted (doc_id=%s)",
                        self._doc_id[:32] if self._doc_id else "",
                    )
                    return
                current = grammar_checker_identity()
                if (
                    self._session_identity is None
                    and self._blob_identity
                    and self._blob_identity != current
                ):
                    log.debug(
                        "[grammar] DocumentPersistence: skip save; unused mismatched blob %s (current %s, doc_id=%s)",
                        self._blob_identity,
                        current,
                        self._doc_id[:32] if self._doc_id else "",
                    )
                    return
                model_id = self._session_identity or self._blob_identity or current
                accessed_fps = set(self._session_accessed)
                ignored_rules_list = list(self._ignored_rules)
                entries_snap = dict(self._entries)

            good_fps: list[str] = []
            bad_map: dict[str, list[dict[str, Any]]] = {}
            for fp in accessed_fps:
                errs = entries_snap.get(fp)
                if errs is None:
                    continue
                if not errs:
                    good_fps.append(fp)
                else:
                    bad_map[fp] = [grammar_proofread_json.compress_error(e) for e in errs]

            payload_dict = {
                "version": GRAMMAR_CACHE_VERSION,
                "model": model_id,
                "good": good_fps,
                "bad": bad_map,
                "ignored_rules": ignored_rules_list,
            }
            payload = json.dumps(payload_dict)
            if len(payload) > 900_000:
                log.warning("[grammar] DocumentPersistence: cache JSON too large (%s bytes), skip write", len(payload))
                return
            set_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, payload)
            log.debug(
                "[grammar] DocumentPersistence: saved %s sentences (%s bytes) to udprop (doc_id=%s, v=%s, model=%s)",
                len(good_fps) + len(bad_map),
                len(payload),
                self._doc_id[:32] if self._doc_id else "",
                GRAMMAR_CACHE_VERSION,
                model_id,
            )
        except Exception as e:
            log.warning("[grammar] DocumentPersistence: save user property failed: %s", e)

    def _teardown(self) -> None:
        if self._teardown_done:
            return
        self._teardown_done = True
        self._unregister_listeners()
        with self._lock:
            self._session_accessed.clear()
            self._entries.clear()
            self._blob_identity = None
            self._session_identity = None
        grammar_registry.remove_persistence(self._doc_id)
        self._model = None

    def get(self, fp: str) -> list[dict[str, Any]] | None:
        if self._teardown_done:
            return None
        with self._lock:
            if self._blob_identity == _V2_LLM_PENDING:
                return None
            if (
                self._session_identity is not None
                and self._blob_identity is not None
                and self._session_identity != self._blob_identity
            ):
                return None
            errs = self._entries.get(fp)
            if errs is not None:
                self._session_accessed.add(fp)
                return [dict(e) for e in errs]
        return None

    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        del locale
        if self._teardown_done:
            return
        with self._lock:
            self._session_accessed.add(fp)
            self._entries[fp] = [dict(e) for e in errors]

    def clear(self) -> None:
        with self._lock:
            self._session_accessed.clear()
            self._entries.clear()
            self._blob_identity = None
            self._session_identity = None

    def clear_and_persist(self) -> None:
        """Drop L2 sentence rows and write an empty udprop blob (Recheck). Keep ignore rules."""
        self.clear()
        self._persist_to_udprops()


def get_persistence(ctx: Any, doc_id: str | None = None, *, model: Any = None) -> DocumentPersistence | None:
    """Return per-document persistence for grammar sentence cache."""
    return grammar_registry.get_persistence(ctx, doc_id, model=model)


def persistence_for_model(model: Any) -> DocumentPersistence | None:
    """Return the persistence bound to *model* (RuntimeUID, then identity)."""
    if model is None:
        return None
    from plugin.framework.uno_context import get_runtime_uid

    uid = get_runtime_uid(model)
    with grammar_registry.lock:
        instances = list(grammar_registry.doc_persistence_instances.values())
    for p in instances:
        other = p._model
        if other is None:
            continue
        if uid:
            other_uid = get_runtime_uid(other)
            if other_uid and other_uid == uid:
                return p
        if other is model:
            return p
    return None


def clear_all_document_persistence(ctx: Any) -> None:
    """Remove every ``DocumentPersistence`` (listeners + map); for tests / reset without doc_id."""
    grammar_registry.clear_all(ctx)


def get_cached_document_locales(ctx: Any, doc_id: str) -> list[str]:
    """Return BCP-47 locales used in *doc_id*, cached for 60 seconds.

    Scans ~1,000 characters around the view cursor (500 behind, 500 ahead) to detect
    locally relevant CharLocale properties without full-document traversal cost.
    """
    now = time.time()
    with grammar_registry.lock:
        cached = grammar_registry.doc_locales_cache.get(doc_id)
    if cached is not None and now - cached[0] < 60:
        return cached[1]

    def _query_locales() -> list[str]:
        locales = set()
        try:
            model = get_document_model_for_id(ctx, doc_id)
            if model:
                ctrl = getattr(model, "getCurrentController", lambda: None)()
                view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None
                if view_cursor:
                    try:
                        tc = view_cursor.getText().createTextCursorByRange(view_cursor)
                        tc.goLeft(500, False)
                        for _unused in range(1000):
                            if not tc.goRight(1, True):
                                break
                            loc = getattr(tc, "CharLocale", None)
                            from . import grammar_proofread_locale

                            bcp = grammar_proofread_locale.normalize_uno_locale_to_bcp47(loc)
                            if bcp:
                                locales.add(bcp)
                            tc.collapseToEnd()
                    except Exception as e:
                        log.debug("[grammar] Failed to scan near cursor for locales: %s", e)

                log.debug("[grammar] Document locale detection finished (cursor scan). Found: %s", locales)
        except Exception as e:
            log.warning("Failed to query document for locales: %s", e)

        if not locales:
            locales.add("en-US")
        return sorted(list(locales))

    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework import queue_executor

        if on_main_thread():
            locs = _query_locales()
        else:
            locs = queue_executor.execute_on_main_thread(_query_locales)
        with grammar_registry.lock:
            grammar_registry.doc_locales_cache[doc_id] = (now, locs)
        return locs
    except Exception as e:
        log.warning("Failed to get cached locales: %s", e)
        return ["en-US"]


def apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str) -> None:
    """Update CharLocale on the sentence text span inside the document when language mismatch occurs."""
    import uno
    from . import grammar_proofread_locale

    def _do_update() -> None:
        model = get_document_model_for_id(ctx, doc_id)
        if not model:
            return

        lang, country = grammar_proofread_locale.bcp47_to_uno_lang_country(
            grammar_proofread_locale.normalize_detected_bcp47(detected_bcp47) or detected_bcp47
        )

        new_locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language=lang, Country=country)

        ctrl = getattr(model, "getCurrentController", lambda: None)()
        view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None

        search_desc = model.createSearchDescriptor()
        search_desc.setSearchString(sentence_text)
        try:
            search_desc.setPropertyValue("SearchCaseSensitive", True)
        except Exception:
            search_desc.SearchCaseSensitive = True

        found_range = None
        if view_cursor:
            found_range = model.findNext(view_cursor.getStart(), search_desc)

        if not found_range:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            try:
                text_obj = model.getText()
                doc_start = text_obj.getStart()
                found_range = model.findNext(doc_start, search_desc)
            except Exception:
                found_range = model.findFirst(search_desc)

        if not found_range:
            found_range = model.findFirst(search_desc)

        if found_range:
            found_range.setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s", detected_bcp47)

    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework import queue_executor

        if on_main_thread():
            _do_update()
        else:
            queue_executor.execute_on_main_thread(_do_update)
    except Exception as e:
        log.warning("Failed to update language property: %s", e)

