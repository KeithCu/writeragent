# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent storage for grammar check results in user-defined document properties.

Per-document persistence stores sentence results in user-defined document properties
and keeps a process-local map keyed by LibreOffice ``aDocumentIdentifier`` (often a
small integer per open doc, not ``RuntimeUID``). The first ``doProofreading`` resolves
the Writer model from the desktop (current text component, or the open Writer this
call is for) and passes it to ``get_persistence(ctx, doc_id, model=...)``. The model
is not read back from this map. A later call does not replace a bound model or reload
udprops over live edits. ``OnUnload`` / dispose removes map entries so instances can
be garbage-collected.

L2 rows are keyed by CharLocale and sentence fingerprint (schema v4). v2 and v3
blobs have no locale; those rows load under a legacy bucket and adopt the first
locale that reads them. Other locales miss. Unknown schema versions are cleared.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import weakref
from typing import Any, NamedTuple

log = logging.getLogger("writeragent.grammar")

from plugin.framework.config import grammar_checker_identity

from plugin.framework.grammar_rule_ids import (
    bcp47_to_uno_lang_country,
    normalize_detected_bcp47,
)

from . import grammar_proofread_json
from .grammar_proofread_locale import (
    GRAMMAR_CACHE_MAX_CHARS,
    GRAMMAR_CACHE_VERSION,
    GRAMMAR_DOC_CACHE_UDPROP,
)

# v2 blobs with LLM-style errors have no model id. First `llm:…` identity to
# touch the document adopts the rows (young-codebase hack; avoids a full recheck).
_V2_LLM_PENDING = "llm"

# v2/v3 rows have no CharLocale. Empty cannot collide with a BCP-47 tag.
# The first locale-specific read adopts the row; other locales miss.
_LEGACY_LOCALE = ""


def _blob_model_id(data: dict[str, Any]) -> str | None:
    """Checker identity on a v3+ blob, or None when ``model`` is missing or not a string.

    ``str([1, 2])`` would become a fake identity and silently drop or mis-bind the cache.
    """
    raw = data.get("model")
    if isinstance(raw, str) and raw:
        return raw
    return None

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

# L1 tuple layout: fingerprint is index 0, errors is index 3. Getters still copy the list themselves.
class SentenceCacheEntry(NamedTuple):
    fingerprint: str
    canonical: str
    complete: bool
    errors: list[dict[str, Any]]


class GrammarRegistry:
    lock: threading.RLock

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.doc_persistence_instances: dict[str, "DocumentPersistence"] = {}
        self.sentence_cache: collections.OrderedDict[str, SentenceCacheEntry] = collections.OrderedDict()
        self.ignored_rules: set[str] = set()
        self.doc_locales: dict[str, set[str]] = {}
        self.lang_detect_cache: collections.OrderedDict[str, str] = collections.OrderedDict()
        self.live_proofreaders: weakref.WeakSet[Any] = weakref.WeakSet()

    def register_live_proofreader(self, proofreader: Any) -> None:
        """Keep the Linguistic-owned proofreader so Settings Recheck can fire PROOFREAD_AGAIN."""
        try:
            self.live_proofreaders.add(proofreader)
        except TypeError:
            log.debug("[grammar] live proofreader is not weak-referenceable")

    def record_enqueued_locale(self, doc_id: str, locale: str) -> None:
        """Record an enqueued locale tag for doc_id."""
        if not doc_id or not locale:
            return
        with self.lock:
            self.doc_locales.setdefault(doc_id, set()).add(locale)

    def get_doc_locales(self, doc_id: str) -> set[str]:
        """Return the set of enqueued locales for doc_id."""
        with self.lock:
            return set(self.doc_locales.get(doc_id, ()))

    def get_persistence(self, ctx: Any, doc_id: str | None, *, model: Any = None) -> DocumentPersistence | None:
        if ctx is None or not doc_id:
            return None
        with self.lock:
            existing = self.doc_persistence_instances.get(doc_id)
            if existing is None:
                # No model here: binding registers UNO listeners, so it runs after the lock.
                existing = DocumentPersistence(ctx, doc_id)
                self.doc_persistence_instances[doc_id] = existing
        if model is not None:
            existing._bind_model(model)
        return existing

    def remove_persistence_if(self, doc_id: str, instance: "DocumentPersistence") -> bool:
        with self.lock:
            if self.doc_persistence_instances.get(doc_id) is instance:
                self.doc_persistence_instances.pop(doc_id, None)
                self.doc_locales.pop(doc_id, None)
                return True
            return False

    def clear_for_doc(self, doc_id: str) -> None:
        with self.lock:
            self.doc_locales.pop(doc_id, None)
            dp = self.doc_persistence_instances.pop(doc_id, None)
        if dp:
            dp._teardown()

    def clear_all(self, ctx: Any | None = None) -> None:
        with self.lock:
            self.sentence_cache.clear()
            self.ignored_rules.clear()
            self.doc_locales.clear()
            self.lang_detect_cache.clear()
            snap = list(self.doc_persistence_instances.values())
            self.doc_persistence_instances.clear()

        for dp in snap:
            dp._teardown()

    def get_cached_language(self, text: str) -> str | None:
        from .grammar_proofread_cache import _normalize_for_sentence_cache

        norm = _normalize_for_sentence_cache(text)
        with self.lock:
            if norm in self.lang_detect_cache:
                self.lang_detect_cache.move_to_end(norm)
                return self.lang_detect_cache[norm]
        return None

    def put_cached_language(self, text: str, lang: str) -> None:
        from .grammar_proofread_cache import _normalize_for_sentence_cache

        norm = _normalize_for_sentence_cache(text)
        with self.lock:
            self.lang_detect_cache[norm] = lang
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
        name = getattr(Event, "EventName", "") or ""
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
        self._session_accessed: set[tuple[str, str]] = set()
        self._ignored_rules: set[str] = set()
        self._lock = threading.Lock()
        self._doc_id = doc_id
        # fingerprint -> locale -> errors. ``_LEGACY_LOCALE`` holds v2/v3 rows.
        self._entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._blob_identity: str | None = None
        self._session_identity: str | None = None
        self._model: Any = None
        self._doc_listener: Any = None
        self._teardown_done = False
        self._load_done: threading.Event = threading.Event()
        if model is not None:
            self._bind_model(model)
        else:
            log.debug("[grammar] DocumentPersistence: no model for doc_id=%s (in-memory only until resolved)", doc_id[:32] if doc_id else "")

    def mark_accessed(self, fp: str, locale: str | None = None) -> None:
        """Record that this fingerprint was used this session (udprop save filter).

        ``locale`` selects the L2 bucket. A legacy v2/v3 row is adopted to that
        locale so a one-language document still round-trips on save.
        """
        with self._lock:
            if locale:
                self._adopt_legacy_locked(fp, locale)
                self._session_accessed.add((locale, fp))
                return
            self._session_accessed.add((_LEGACY_LOCALE, fp))

    def _bind_model(self, model: Any) -> None:
        """Attach the Writer model after init when ``get_persistence(..., model=...)`` runs."""
        if self._teardown_done or model is None:
            return
        with self._lock:
            if self._teardown_done:
                return
            if self._model is not None:
                self._load_done.wait()
                return
            self._model = model
        try:
            self._load_from_udprops()
        finally:
            self._load_done.set()
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

    def _adopt_legacy_locked(self, fp: str, locale: str) -> None:
        """Bind a locale-blind row to ``locale`` when it is the only bucket.

        Caller holds ``_lock``. v2/v3 blobs have no CharLocale. Serving that
        row to every locale returns the wrong errors (or a false "good" hit).
        Adopting it on the first locale that asks keeps single-language
        documents working; any other locale misses and is proofread.
        """
        if not locale:
            return
        by_locale = self._entries.get(fp)
        if not by_locale or locale in by_locale:
            return
        if set(by_locale) != {_LEGACY_LOCALE}:
            return
        by_locale[locale] = by_locale.pop(_LEGACY_LOCALE)

    def _clear_rows_for_identity_switch_locked(self) -> None:
        """Drop sentence rows and ignore ids from the previous checker.

        Caller holds ``_lock``. Ignore-rule ids belong to that checker's
        errors. Keeping them after a switch re-persists rules for errors that
        were just discarded. Recheck uses ``clear`` and keeps ignore rules.
        """
        self._entries.clear()
        self._session_accessed.clear()
        self._ignored_rules.clear()

    def _reject_unknown_cache(self, version: object) -> None:
        """Drop a blob whose schema this build does not know how to read."""
        log.warning(
            "[grammar] DocumentPersistence: unknown cache version %r on doc_id=%s; clearing",
            version,
            self._doc_id[:32] if self._doc_id else "",
        )
        with self._lock:
            self._entries.clear()
            self._ignored_rules.clear()
            self._session_accessed.clear()
            self._blob_identity = None

    def _load_from_udprops(self) -> None:
        from plugin.doc.udprops import get_document_property

        if not self._model:
            return
        try:
            raw = get_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, None)
            if not raw or not isinstance(raw, str):
                log.debug("[grammar] DocumentPersistence: no cached property on doc_id=%s", self._doc_id[:32] if self._doc_id else "")
                return
            if len(raw) > GRAMMAR_CACHE_MAX_CHARS:
                self._reject_unknown_cache(f"length {len(raw)} > {GRAMMAR_CACHE_MAX_CHARS}")
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                log.warning("[grammar] DocumentPersistence: load user property JSON decode error: %s", e)
                return
            if not isinstance(data, dict):
                return

            version = data.get("version", 1)
            # Future schemas must not fall through into the v3/v4 reader.
            # ``bool`` is an ``int`` subclass; reject it with other non-ints.
            if type(version) is not int:
                self._reject_unknown_cache(version)
                return
            current = grammar_checker_identity()
            load_entries = False
            blob_identity: str | None = None
            locale_aware = False

            if version == 4:
                locale_aware = True
                blob_identity = _blob_model_id(data)
                load_entries = blob_identity is not None and blob_identity == current
            elif version == 3:
                blob_identity = _blob_model_id(data)
                load_entries = blob_identity is not None and blob_identity == current
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
            elif version < 2:
                log.debug(
                    "[grammar] DocumentPersistence: ignoring old-version cache (v=%s) on doc_id=%s",
                    version,
                    self._doc_id[:32] if self._doc_id else "",
                )
                return
            else:
                self._reject_unknown_cache(version)
                return

            decoded_entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
            decoded_ignored: set[str] = set()
            dropped_count = 0

            def _fill_decoded_from_payload(payload: dict[str, Any], loc: str) -> None:
                nonlocal dropped_count
                good = payload.get("good")
                if isinstance(good, list):
                    for fp in good:
                        if isinstance(fp, str):
                            decoded_entries.setdefault(fp, {})[loc] = []
                        else:
                            dropped_count += 1
                elif good is not None:
                    dropped_count += 1

                bad = payload.get("bad")
                if isinstance(bad, dict):
                    for fp, compressed_errors in bad.items():
                        if not isinstance(fp, str) or not isinstance(compressed_errors, list):
                            dropped_count += 1
                            continue
                        errs: list[dict[str, Any]] = []
                        for e in compressed_errors:
                            if isinstance(e, dict):
                                errs.append(grammar_proofread_json.decompress_error(e))
                            else:
                                dropped_count += 1
                        if not errs:
                            dropped_count += 1
                            continue
                        decoded_entries.setdefault(fp, {})[loc] = errs
                elif bad is not None:
                    dropped_count += 1

            if load_entries:
                raw_ignored = data.get("ignored_rules")
                if isinstance(raw_ignored, list):
                    for r in raw_ignored:
                        if isinstance(r, str):
                            decoded_ignored.add(r)
                        else:
                            dropped_count += 1
                elif raw_ignored is not None:
                    dropped_count += 1

                if locale_aware:
                    locales = data.get("locales")
                    if isinstance(locales, dict):
                        for loc, bucket in locales.items():
                            if not isinstance(loc, str) or not loc or not isinstance(bucket, dict):
                                continue
                            _fill_decoded_from_payload(bucket, loc)
                else:
                    _fill_decoded_from_payload(data, _LEGACY_LOCALE)

            # Swap/merge under _lock at the end
            with self._lock:
                if not load_entries:
                    if self._session_identity is None or self._session_identity == blob_identity:
                        self._entries.clear()
                        self._ignored_rules.clear()
                        self._blob_identity = blob_identity
                else:
                    if self._session_identity is not None and self._session_identity != blob_identity:
                        # Session identity mismatch: do not import blob rows or ignore rules.
                        pass
                    else:
                        self._blob_identity = blob_identity
                        if not self._ignored_rules:
                            self._ignored_rules = decoded_ignored
                        if not self._entries:
                            self._entries = decoded_entries
                        else:
                            for fp, by_loc in decoded_entries.items():
                                target_by_loc = self._entries.setdefault(fp, {})
                                for loc, errs in by_loc.items():
                                    if loc not in target_by_loc:
                                        target_by_loc[loc] = errs

                loaded_count = sum(len(by_loc) for by_loc in self._entries.values())

            log.debug(
                "[grammar] DocumentPersistence: loaded %s sentences (%s dropped) from udprop (doc_id=%s, v=%s, blob=%s, current=%s)",
                loaded_count,
                dropped_count,
                self._doc_id[:32] if self._doc_id else "",
                version,
                blob_identity,
                current,
            )
        except Exception:
            log.exception("[grammar] DocumentPersistence: load user property failed")

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
                    self._clear_rows_for_identity_switch_locked()
                    self._blob_identity = identity
                elif not blob:
                    self._blob_identity = identity
                return None

            if previous == identity:
                return None

            self._clear_rows_for_identity_switch_locked()
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
                accessed_pairs = set(self._session_accessed)
                ignored_rules_list = list(self._ignored_rules)
                accessed_triples: list[tuple[str, str, list[dict[str, Any]]]] = []
                for locale, fp in accessed_pairs:
                    # Legacy rows without a CharLocale are never rewritten (locale-blind bug).
                    if not locale:
                        continue
                    by_locale = self._entries.get(fp)
                    if by_locale is None:
                        continue
                    errs = by_locale.get(locale)
                    if errs is not None:
                        accessed_triples.append((locale, fp, list(errs)))

            good_by_locale: dict[str, list[str]] = {}
            bad_by_locale: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for locale, fp, errs in accessed_triples:
                if not errs:
                    good_by_locale.setdefault(locale, []).append(fp)
                else:
                    bad_by_locale.setdefault(locale, {})[fp] = [
                        grammar_proofread_json.compress_error(e) for e in errs
                    ]

            locales_out: dict[str, dict[str, Any]] = {}
            for locale in set(good_by_locale) | set(bad_by_locale):
                locales_out[locale] = {
                    "good": good_by_locale.get(locale, []),
                    "bad": bad_by_locale.get(locale, {}),
                }
            n_saved = sum(len(bucket["good"]) + len(bucket["bad"]) for bucket in locales_out.values())

            payload_dict = {
                "version": GRAMMAR_CACHE_VERSION,
                "model": model_id,
                "locales": locales_out,
                "ignored_rules": ignored_rules_list,
            }
            payload = json.dumps(payload_dict)
            if len(payload) > GRAMMAR_CACHE_MAX_CHARS:
                log.warning("[grammar] DocumentPersistence: cache JSON too large (%s chars), skip write", len(payload))
                return
            set_document_property(self._model, GRAMMAR_DOC_CACHE_UDPROP, payload)
            log.debug(
                "[grammar] DocumentPersistence: saved %s sentences (%s chars) to udprop (doc_id=%s, v=%s, model=%s)",
                n_saved,
                len(payload),
                self._doc_id[:32] if self._doc_id else "",
                GRAMMAR_CACHE_VERSION,
                model_id,
            )
        except Exception:
            log.exception("[grammar] DocumentPersistence: save user property failed")

    def _teardown(self) -> None:
        if self._teardown_done:
            return
        self._teardown_done = True
        self._load_done.set()
        self._unregister_listeners()
        with self._lock:
            self._session_accessed.clear()
            self._entries.clear()
            self._blob_identity = None
            self._session_identity = None
        grammar_registry.remove_persistence_if(self._doc_id, self)
        self._model = None

    def get(
        self,
        fp: str,
        locale: str | None = None,
        *,
        adopt_legacy: bool = True,
    ) -> list[dict[str, Any]] | None:
        """Return errors for ``fp`` under ``locale``.

        L1 keys are ``sent|locale|identity|fp``. L2 used to key on ``fp`` only,
        so the same sentence under two CharLocales reused the other locale's
        errors or a good (empty) row. Pass ``locale`` for a real lookup.

        A single legacy v2/v3 bucket is adopted to ``locale`` when
        ``adopt_legacy`` is true (proofreading). Probes pass false so a
        language-detect check cannot retag that row. Without ``locale``,
        return a row only when exactly one bucket exists.
        """
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
            by_locale = self._entries.get(fp)
            if not by_locale:
                return None
            if locale:
                if adopt_legacy:
                    self._adopt_legacy_locked(fp, locale)
                errs = (self._entries.get(fp) or {}).get(locale)
                if errs is None:
                    return None
                self._session_accessed.add((locale, fp))
                return [dict(e) for e in errs]
            if len(by_locale) != 1:
                return None
            only_locale, errs = next(iter(by_locale.items()))
            self._session_accessed.add((only_locale, fp))
            return [dict(e) for e in errs]

    def put(self, fp: str, locale: str, errors: list[dict[str, Any]]) -> None:
        """Store ``errors`` for this fingerprint under ``locale``."""
        if self._teardown_done:
            return
        locale_key = locale or _LEGACY_LOCALE
        with self._lock:
            by_locale = self._entries.setdefault(fp, {})
            # A located write retires the blind row so another locale cannot adopt it.
            if locale_key:
                by_locale.pop(_LEGACY_LOCALE, None)
            by_locale[locale_key] = [dict(e) for e in errors]
            self._session_accessed.add((locale_key, fp))

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

    def ignored_rules_snapshot(self) -> set[str]:
        """Return a copy of document-scoped ignored rules under lock."""
        with self._lock:
            return set(self._ignored_rules)

    def add_ignored_rule(self, rule_key: str) -> None:
        """Add a document-scoped ignored rule under lock and persist to udprops."""
        with self._lock:
            self._ignored_rules.add(rule_key)
        self._persist_to_udprops()

    def clear_ignored_rules(self) -> None:
        """Clear document-scoped ignored rules under lock and persist to udprops."""
        with self._lock:
            self._ignored_rules.clear()
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


def _get_paragraph_text_for_range(model: Any, rng: Any) -> str | None:
    """Return the entire paragraph string containing rng, or None."""
    try:
        text_obj = getattr(rng, "getText", lambda: None)()
        if text_obj is None and model is not None:
            text_obj = getattr(model, "getText", lambda: None)()
        if text_obj is not None:
            tc = text_obj.createTextCursorByRange(rng)
            if hasattr(tc, "gotoStartOfParagraph") and hasattr(tc, "gotoEndOfParagraph"):
                tc.gotoStartOfParagraph(False)
                tc.gotoEndOfParagraph(True)
                return tc.getString()
    except Exception as exc:
        log.debug("paragraph text for range failed (%s)", type(exc).__name__)
    return None


def apply_language_change(
    ctx: Any,
    doc_id: str,
    sentence_text: str,
    detected_bcp47: str,
    start_pos: int = 0,
    paragraph_text: str = "",
) -> bool:
    """Update CharLocale on the sentence text span inside the document when language mismatch occurs."""
    import uno

    def _do_update() -> bool:
        model = get_document_model_for_id(ctx, doc_id)
        if not model:
            return False

        lang, country = bcp47_to_uno_lang_country(
            normalize_detected_bcp47(detected_bcp47) or detected_bcp47
        )

        new_locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language=lang, Country=country)

        ctrl = getattr(model, "getCurrentController", lambda: None)()
        view_cursor = getattr(ctrl, "getViewCursor", lambda: None)() if ctrl else None

        found_range = None
        # Paragraph-relative cursor check: verify sentence text and paragraph text if provided.
        # The paragraph-relative cursor is built for every start_pos,
        # including 0. n_start is the sentence offset inside the
        # proofread paragraph, so 0 is its first sentence, not "search
        # from the caret". findNext from the view cursor retags a
        # different copy of the same sentence when 0 is treated as "no
        # offset". Offset 0 still selects from the start of that
        # paragraph. start_pos > 0 still moves, then expands, on the same
        # cursor.
        if view_cursor is not None and start_pos >= 0:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            text_obj = model.getText()
            doc_cursor = text_obj.createTextCursorByRange(view_cursor.getStart())
            if hasattr(doc_cursor, "gotoStartOfParagraph"):
                doc_cursor.gotoStartOfParagraph(False)
                if start_pos > 0:
                    doc_cursor.goRight(start_pos, False)
                doc_cursor.goRight(len(sentence_text), True)
                if doc_cursor.getString() == sentence_text:
                    if not paragraph_text:
                        found_range = doc_cursor
                    else:
                        para_str = _get_paragraph_text_for_range(model, doc_cursor)
                        if para_str == paragraph_text:
                            found_range = doc_cursor

        if found_range:
            found_range.setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s", detected_bcp47)
            return True

        # Fast path missed; fallback to findAll. Keep a hit only when that
        # range's paragraph string equals paragraph_text. One remaining hit is retagged.
        search_desc = model.createSearchDescriptor()
        search_desc.setSearchString(sentence_text)
        search_desc.SearchCaseSensitive = True

        matches = model.findAll(search_desc)
        matched_ranges = []
        if matches is not None:
            count = getattr(matches, "getCount", lambda: 0)()
            for i in range(count):
                rng = matches.getByIndex(i)
                if paragraph_text:
                    para_str = _get_paragraph_text_for_range(model, rng)
                    if para_str == paragraph_text:
                        matched_ranges.append(rng)
                else:
                    matched_ranges.append(rng)

        if len(matched_ranges) == 1:
            matched_ranges[0].setPropertyValue("CharLocale", new_locale)
            log.info("[grammar] Updated CharLocale for sentence to %s (via findAll)", detected_bcp47)
            return True

        return False

    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework import queue_executor

        if on_main_thread():
            return bool(_do_update())
        else:
            return bool(queue_executor.execute_on_main_thread(_do_update))
    except Exception as e:
        log.warning("Failed to update language property: %s", e)
        return False

