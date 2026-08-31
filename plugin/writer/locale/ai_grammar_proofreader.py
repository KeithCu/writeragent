# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Linguistic2 grammar checker (Lightproof-style): XProofreader backed by LLM + cache.

Architecture and module map: ``docs/writer/grammar-checker-plan.md``.
UNO service ``__init__(self, ctx, *args)`` is required (LibreOffice uses
``createInstanceWithArgumentsAndContext``). Keep top-level imports minimal —
see stdlib-only bootstrap above before ``plugin.*`` imports.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

# Minimal stdlib-only bootstrap (must run before the "from plugin..." import below)
# because unopkg writeRegistryInfo loads this file before the OXT root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(4):  # writer/locale/ai_grammar_proofreader.py → writer/locale/ → writer/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

# Ensure we can do normal plugin.* imports even when LO loads this file
# directly as a standalone UNO component (XProofreader service).
from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=4, also_add_lib=True)

from typing import Any, Sequence, cast

import unohelper

from com.sun.star.lang import XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales, XLinguServiceEventBroadcaster

log = logging.getLogger("writeragent.grammar")

IMPLEMENTATION_NAME = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"
SERVICE_NAME = "com.sun.star.linguistic2.Proofreader"

uno_mod: Any


def _advance_past_leading_whitespace(text: str, index: int) -> int:
    """Advance ``index`` while ``text[index]`` is Unicode whitespace (not ASCII space only)."""
    n = min(max(0, index), len(text))
    while n < len(text) and text[n].isspace():
        n += 1
    return n


try:
    uno_mod = importlib.import_module("uno")
except ImportError:
    uno_mod = None

# INFO once when grammar is off (Writer still calls doProofreading); reset when enabled again.
_GRAMMAR_DISABLED_NOTICE_EMITTED = False

from plugin.writer.locale.grammar_proofread_cache import cache_get_sentence, ignore_rule_add, ignore_rules_clear
from plugin.writer.locale.grammar_proofread_locale import (
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_uno_lang_country,
    grammar_inflight_key,
    looks_complete_sentence,
    normalize_uno_locale_to_bcp47,
)
from plugin.writer.locale.grammar_proofread_text import (
    NormalizedProofError,
    active_spans_from_paragraph,
    calculate_covered_span_end,
    candidate_sentence_spans_for_proofreading,
    filter_sentence_spans_for_thresholds,
    reconcile_active_and_paragraph_spans,
    slice_preview_debug,
)
from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkItem,
    emit_grammar_status,
    grammar_obs,
    grammar_queue,
    next_enqueue_seq,
)


def _run_on_main_thread(fn, *args, **kwargs):
    """Run *fn* on the LO UI thread (XProofreader hooks run on linguistic workers)."""
    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.thread_guard import on_main_thread

    if on_main_thread():
        return fn(*args, **kwargs)
    return execute_on_main_thread(fn, *args, **kwargs)


def _ensure_persistence_bound(ctx: Any, doc_id: str | None) -> None:
    """Bind ``DocumentPersistence`` to the Writer model (loads udprops when available)."""
    if not doc_id:
        return
    from plugin.framework.uno_context import get_active_document
    from plugin.writer.locale.grammar_persistence import get_document_model_for_id, get_persistence

    model = get_document_model_for_id(ctx, doc_id)
    if model is None:
        model = get_active_document(ctx)
    get_persistence(ctx, doc_id, model=model)


def _add_doc_ignored_rule(p: Any, value: str) -> None:
    with p._lock:
        p._ignored_rules.add(value)
    p._persist_to_udprops()


def _ignore_rule_on_main(ctx: Any, doc_id: str | None, rule_identifier: str) -> None:
    _ensure_persistence_bound(ctx, doc_id)
    ignore_rule_add(str(rule_identifier))
    from plugin.writer.locale.grammar_persistence import get_persistence

    p = get_persistence(ctx, doc_id) if doc_id else None
    if not p:
        return
    from .grammar_ignore_rules import canonical_rule_keys

    stored = canonical_rule_keys(rule_identifier)[0]
    _add_doc_ignored_rule(p, stored)
    log.debug("[grammar] ignoreRule added: '%s' (stored: '%s') to doc_id=%s", rule_identifier, stored, doc_id)


def _reset_ignore_rules_on_main(ctx: Any, doc_id: str | None) -> None:
    _ensure_persistence_bound(ctx, doc_id)
    ignore_rules_clear()
    from plugin.writer.locale.grammar_persistence import get_persistence

    p = get_persistence(ctx, doc_id) if doc_id else None
    if not p:
        return
    with p._lock:
        p._ignored_rules.clear()
    p._persist_to_udprops()
    log.debug("[grammar] resetIgnoreRules cleared all ignored rules for doc_id=%s", doc_id)


def _proofreading_markup_type() -> int:
    """``com.sun.star.text.TextMarkupType.PROOFREADING`` via PyUNO constant lookup."""
    if uno_mod is None:
        return 0
    try:
        v: Any = uno_mod.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")
        return int(cast("int | float | str", v))
    except Exception as e:
        log.warning("[grammar] _proofreading_markup_type: falling back to 4: %s", e, exc_info=True)
        return 4


def _cached_errors_to_uno_tuple(cached: tuple[dict[str, Any], ...], ctx: Any, doc_id: str) -> tuple[Any, ...]:
    from plugin.writer.locale.grammar_ignore_rules import doc_ignored_rules, is_rule_ignored
    from plugin.writer.locale.grammar_proofread_cache import ignored_rules_snapshot

    doc_ignored = doc_ignored_rules(ctx, doc_id)
    global_ignored = ignored_rules_snapshot()

    norms = []
    for d in cached:
        rule_ident = str(d.get("rule_identifier", ""))
        if is_rule_ignored(rule_ident, doc_ignored, global_ignored):
            continue

        norms.append(
            NormalizedProofError(
                n_error_start=int(d["n_error_start"]),
                n_error_length=int(d["n_error_length"]),
                suggestions=tuple(d.get("suggestions") or ()),
                short_comment=str(d.get("short_comment", "")),
                full_comment=str(d.get("full_comment", "")),
                rule_identifier=rule_ident
            )
        )
    return _errors_to_uno_tuple(norms)


def _locale_key(loc: Any) -> str:
    try:
        return f"{loc.Language}_{loc.Country}_{loc.Variant}"
    except Exception as e:
        log.debug("[grammar] _locale_key: %s", e, exc_info=True)
        return "unknown"


def _locale_tuple() -> tuple[Any, ...]:
    """Locales returned by ``getLocales`` — must match ``LinguisticWriterAgentGrammar.xcu`` ``Locales``.

    The XCU uses hyphenated BCP47-like tags in one ``oor:string-list`` value; UNO uses
    ``com.sun.star.lang.Locale`` in the same order as ``GRAMMAR_REGISTRY_LOCALE_TAGS``.

    LibreOffice merges the registry list with ``XSupportedLocales``; an extra locale here that is
    not listed under GrammarCheckers in the XCU has been observed to trigger a UNO RuntimeException
    when opening Tools → Options → Language Settings (Writing aids).
    """
    if uno_mod is None:
        return ()
    out: list[Any] = []
    try:
        for tag in GRAMMAR_REGISTRY_LOCALE_TAGS:
            la, ctry = bcp47_to_uno_lang_country(tag)
            out.append(cast("Any", uno_mod.createUnoStruct("com.sun.star.lang.Locale", Language=la, Country=ctry, Variant="")))
        return tuple(out)
    except Exception:
        log.exception("[grammar] _locale_tuple: Locale construction failed")
        return ()


def ensure_writeragent_proofreader_configured(ctx: Any) -> None:
    """Log Doc-tab grammar state and start background warmup if local engine is configured.

    We intentionally do **not** call ``XLinguServiceManager2.setConfiguredServices`` here: doing that
    during startup/sidebar init has been observed to destabilize LibreOffice (Writing aids / proofreader
    list). The Linguistic ``GrammarCheckers`` XCU is bundled in the default OXT; users still pick the
    active grammar checker under Tools → Options → Language Settings → Writing aids.
    """
    from plugin.framework.logging import init_logging
    from plugin.framework.config import init_config, is_grammar_enabled, user_config_dir

    init_logging(ctx)
    log.debug("[grammar] ensure_proofreader_selection: entry")
    try:
        init_config(ctx)
    except Exception as e:
        log.debug("[grammar] ensure_proofreader_selection: init_config: %s", e)
    enabled = is_grammar_enabled()
    if not enabled:
        log.info("[grammar] ensure_proofreader_selection: Doc-tab AI grammar off (enable on Doc tab to use the checker)")
        return
    try:
        ucd = user_config_dir() or ""
    except Exception:
        ucd = ""
    if not ucd:
        log.debug("[grammar] skip harper warmup: user config dir not ready")
        return
    try:
        from plugin.writer.locale.harper import maybe_start_harper_async

        maybe_start_harper_async(ctx, user_config_dir=ucd)
    except Exception as e:
        log.warning("[grammar] OnStartApp: could not warmup harper: %s", e)
    log.info("[grammar] Doc-tab AI grammar on — if Writer does not underline yet, set WriterAgent as the active grammar checker under Tools → Options → Language Settings → Writing aids for the document language (same locales as the extension’s UI translation set).")


def _create_empty_result(proofreader: Any, a_document_identifier: Any, a_text: str, a_locale: Any, n_start_of_sentence_position: int, n_suggested_behind_end_of_sentence_position: int) -> Any:
    """Initialize ProofreadingResult (sentence bounds aligned with Lightproof)."""
    if uno_mod is None:
        raise RuntimeError("uno not available")
    try:
        a_res = cast("Any", uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult"))
        a_res.aDocumentIdentifier = a_document_identifier
        a_res.aText = a_text
        a_res.aLocale = a_locale
        a_res.nStartOfSentencePosition = n_start_of_sentence_position
        a_res.nStartOfNextSentencePosition = n_suggested_behind_end_of_sentence_position
        a_res.aProperties = ()
        a_res.xProofreader = proofreader
        a_res.aErrors = ()

        # Default: follow LO’s suggested end + Lightproof-style space adjustment (see
        # ``_apply_proofreading_end_positions`` when we cover a computed sentence span).
        n_next = n_suggested_behind_end_of_sentence_position
        if n_next < len(a_text):
            before = n_next
            n_next = _advance_past_leading_whitespace(a_text, n_next)
            ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
            if n_next == before and ch != "":
                n_next = n_suggested_behind_end_of_sentence_position + 1
        a_res.nStartOfNextSentencePosition = n_next
        a_res.nBehindEndOfSentencePosition = n_next
        return a_res
    except Exception as e:
        log.exception("[grammar] _create_empty_result failed: %s", e)
        raise


def _apply_proofreading_end_positions(a_res: Any, a_text: str, covered_end: int) -> None:
    """Set traversal positions from the one-past-end of the span we actually checked (sentence-sized).

    Skips spaces after ``covered_end`` so Writer advances past inter-sentence whitespace.
    """
    n_next = min(max(0, covered_end), len(a_text))
    n_next = _advance_past_leading_whitespace(a_text, n_next)
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next


def classify_errors_against_window(
    errors: Sequence[dict[str, Any]], n_start: int, n_behind: int
) -> dict[str, Any]:
    """Count returned errors fully inside / before / after / straddling ``[n_start, n_behind)``.

    Writer honors ``aErrors`` outside that window; this is debug observation
    only (``do_proofreading_result_window``).
    """
    in_w = before = after = straddle = 0
    parts: list[str] = []
    for e in errors:
        start = int(e.get("n_error_start", 0) or 0)
        length = int(e.get("n_error_length", 0) or 0)
        end = start + length
        if start >= n_start and end <= n_behind:
            in_w += 1
            where = "in"
        elif end <= n_start:
            before += 1
            where = "before"
        elif start >= n_behind:
            after += 1
            where = "after"
        else:
            straddle += 1
            where = "straddle"
        parts.append(f"{start}+{length}:{where}")
    return {
        "in_window": in_w,
        "before_window": before,
        "after_window": after,
        "straddle": straddle,
        "error_spans": ",".join(parts),
    }


def _obs_result_window(
    doc_id: str,
    loc_key: str,
    a_res: Any,
    combined_errors: Sequence[dict[str, Any]],
    *,
    paragraph_span_count: int,
    active_span_count: int,
    uncached_active_count: int,
) -> None:
    n_start = int(getattr(a_res, "nStartOfSentencePosition", 0) or 0)
    n_behind = int(getattr(a_res, "nBehindEndOfSentencePosition", 0) or 0)
    n_next = int(getattr(a_res, "nStartOfNextSentencePosition", 0) or 0)
    cls = classify_errors_against_window(combined_errors, n_start, n_behind)
    grammar_obs(
        "do_proofreading_result_window",
        doc_id=doc_id,
        grammar_bcp47=loc_key,
        n_start=n_start,
        n_behind=n_behind,
        n_next=n_next,
        n_errors=len(combined_errors),
        paragraph_spans=paragraph_span_count,
        active_spans=active_span_count,
        uncached_active=uncached_active_count,
        **cls,
    )


def _errors_to_uno_tuple(norms: Sequence[NormalizedProofError]) -> tuple[Any, ...]:
    if uno_mod is None:
        return ()
    out: list[Any] = []
    for idx, e in enumerate(norms):
        try:
            a_err = cast("Any", uno_mod.createUnoStruct("com.sun.star.linguistic2.SingleProofreadingError"))
            a_err.nErrorStart = e.n_error_start
            a_err.nErrorLength = e.n_error_length
            a_err.nErrorType = _proofreading_markup_type()
            a_err.aRuleIdentifier = e.rule_identifier
            a_err.aSuggestions = tuple(e.suggestions)
            a_err.aShortComment = e.short_comment
            a_err.aFullComment = e.full_comment
            a_err.aProperties = ()
            out.append(a_err)
        except Exception as ex:
            log.warning("[grammar] _errors_to_uno_tuple: skipped error index=%s rule=%r: %s", idx, getattr(e, "rule_identifier", ""), ex, exc_info=True)
    return tuple(out)


class WriterAgentAiGrammarProofreader(unohelper.Base, XProofreader, XServiceInfo, XServiceName, XServiceDisplayName, XSupportedLocales, XLinguServiceEventBroadcaster):  # pyright: ignore[reportGeneralTypeIssues] — multiple UNO interface bases  # pyrefly: ignore[invalid-inheritance]
    """Grammar checker registered under Linguistic / GrammarCheckers (cf. Lightproof)."""

    def __init__(self, ctx: Any, *args: Any):
        # LibreOffice's Linguistic manager instantiates proofreaders with
        # compatibility arguments before querying XSupportedLocales.
        del args
        super().__init__()
        self.ctx = ctx
        self._last_doc_id: str | None = None
        self._lingu_listeners: list[Any] = []
        from plugin.framework.logging import init_logging
        from plugin.writer.locale.grammar_persistence import grammar_registry

        init_logging(ctx)
        grammar_registry.register_live_proofreader(self)
        self._implementation_name = IMPLEMENTATION_NAME
        self._supported_service_names = (SERVICE_NAME,)
        try:
            self._locales = _locale_tuple()
        except Exception:
            log.exception("[grammar] WriterAgentAiGrammarProofreader.__init__: _locale_tuple failed")
            self._locales = ()

    # --- XServiceName / XServiceInfo ---
    def getServiceName(self) -> str:
        return self._implementation_name

    def getImplementationName(self) -> str:
        return self._implementation_name

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self._supported_service_names

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return self._supported_service_names

    # --- XSupportedLocales ---
    def _normalize_locale(self, a_locale: Any) -> str | None:
        """Return the grammar locale used by this proofreader implementation."""
        return normalize_uno_locale_to_bcp47(a_locale)

    def hasLocale(self, aLocale: Any) -> bool:
        try:
            if aLocale is None or not self._locales:
                return False
            return self._normalize_locale(aLocale) is not None
        except Exception as e:
            log.warning("[grammar] hasLocale: %s", e, exc_info=True)
            return False

    def getLocales(self) -> tuple[Any, ...]:
        try:
            return self._locales
        except Exception as e:
            log.warning("[grammar] getLocales: %s", e, exc_info=True)
            return ()

    def _check_enabled_and_locale(self, a_doc_id: str, a_text: str, a_locale: Any, n_start: int, n_suggested_end: int) -> str | None:
        """Return BCP47 locale if grammar checking is enabled and locale is supported, else None."""
        from plugin.framework.config import is_grammar_enabled

        enabled = is_grammar_enabled()

        loc_raw = _locale_key(a_locale)
        grammar_bcp47 = self._normalize_locale(a_locale)

        if not enabled:
            global _GRAMMAR_DISABLED_NOTICE_EMITTED
            if not _GRAMMAR_DISABLED_NOTICE_EMITTED:
                _GRAMMAR_DISABLED_NOTICE_EMITTED = True
                log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
            # Commented out to avoid excessive noise in debug logs when the AI grammar checker is disabled
            # grammar_obs("do_proofreading_skip", reason="grammar_disabled", doc_id=a_doc_id, len_aText=len(a_text), n_start_lo=n_start, n_suggested_behind_end=n_suggested_end, locale_raw=loc_raw)
            return None

        _GRAMMAR_DISABLED_NOTICE_EMITTED = False
        if grammar_bcp47 is None:
            grammar_obs("do_proofreading_skip", reason="locale_not_registered", doc_id=a_doc_id, len_aText=len(a_text), n_start_lo=n_start, n_suggested_behind_end=n_suggested_end, locale_raw=loc_raw)
            return None

        grammar_obs("do_proofreading_entry", doc_id=a_doc_id, len_aText=len(a_text), n_start_lo=n_start, n_suggested_behind_end=n_suggested_end, grammar_bcp47=grammar_bcp47, locale_raw=loc_raw, text_preview=slice_preview_debug(a_text))
        return grammar_bcp47

    def _resolve_work_spans(
        self,
        a_doc_id: str,
        loc_key: str,
        a_text: str,
        n_start: int,
        n_suggested_end: int,
        paragraph_spans: list[tuple[int, int, str]],
    ) -> list[tuple[int, int, str]]:
        """Active-window spans from the already-split, threshold-filtered paragraph.

        One BreakIterator pass happens in ``doProofreading``; this only overlap-filters
        (or returns all spans when ``n_start == 0``). ``raw_candidates`` is the
        post-threshold paragraph count.
        """
        work_spans = active_spans_from_paragraph(paragraph_spans, a_text, n_start, n_suggested_end)
        if not work_spans:
            grammar_obs(
                "do_proofreading_skip",
                reason="no_eligible_sentences_or_incomplete_short",
                doc_id=a_doc_id,
                n_start_lo=n_start,
                raw_candidates=len(paragraph_spans),
                grammar_bcp47=loc_key,
            )
            return []
        return work_spans

    def _process_cache_hits(self, a_doc_id: str, loc_key: str, work_spans: list[tuple[int, int, str]]) -> tuple[list[dict[str, Any]], list[tuple[int, int, str]]]:
        """Check cache for spans; return (combined_errors, uncached_spans)."""
        combined_errors: list[dict[str, Any]] = []
        uncached_spans: list[tuple[int, int, str]] = []
        checker_ident = getattr(self, "_checker_identity", None)
        for sent_start, _sent_end, sent_text in work_spans:
            get_kwargs: dict[str, Any] = {"ctx": self.ctx, "doc_id": a_doc_id}
            if checker_ident:
                get_kwargs["checker_identity"] = checker_ident
            cached = cache_get_sentence(loc_key, sent_text, **get_kwargs)
            grammar_obs("do_proofreading_sentence_cache", doc_id=a_doc_id, sent_start=sent_start, sent_len=len(sent_text), cache_hit=cached is not None, sent_preview=slice_preview_debug(sent_text, 48))
            if cached is None:
                uncached_spans.append((sent_start, _sent_end, sent_text))
                continue
            for err_item in cached:
                adj = dict(err_item)
                adj["n_error_start"] = sent_start + err_item.get("n_error_start", 0)
                combined_errors.append(adj)
        return combined_errors, uncached_spans

    def _active_grammar_provider(self) -> str:
        """Pinned instance provider (LibreHarper) or current config (WriterAgent)."""
        from plugin.framework.config import get_grammar_provider

        return getattr(self, "_provider", "") or get_grammar_provider()

    def _try_harper_fast_path(
        self,
        a_doc_id: str,
        loc_key: str,
        uncached_spans: list[tuple[int, int, str]],
        combined_errors: list[dict[str, Any]],
    ) -> bool:
        """If this is Harper, lint ready process now or kick one ensure. True = do not enqueue."""
        from plugin.framework.config import user_config_dir

        if self._active_grammar_provider() != "harper":
            return False

        from dataclasses import asdict

        from plugin.writer.locale.grammar_ignore_rules import doc_ignored_rules, is_rule_ignored
        from plugin.writer.locale.grammar_proofread_cache import cache_put_sentence, ignored_rules_snapshot
        from plugin.writer.locale.grammar_proofread_text import normalize_errors_for_text
        from plugin.writer.locale.harper import harper_try_lint

        cfg_dir = user_config_dir() or ""
        ident = getattr(self, "_checker_identity", "harper")
        first_text = uncached_spans[0][2] if uncached_spans else ""
        if first_text:
            emit_grammar_status("start", first_text, result="Harper")
        last_text = first_text
        issue_count = 0
        ensuring = False
        for sent_start, unused_end, sent_text in uncached_spans:
            del unused_end
            last_text = sent_text
            res = harper_try_lint(sent_text, cfg_dir, bcp47=loc_key)
            if res is None:
                grammar_obs("do_proofreading_harper_ensure", doc_id=a_doc_id, grammar_bcp47=loc_key)
                emit_grammar_status("request", sent_text, result="Starting Harper…")
                ensuring = True
                break
            emit_grammar_status("request", sent_text, result="Harper check")
            errors = res.get("errors", [])
            ignored = doc_ignored_rules(self.ctx, a_doc_id)
            global_ignored = ignored_rules_snapshot()
            norm_errors = normalize_errors_for_text(sent_text, 0, len(sent_text), errors, self.ctx, loc_key)
            filtered = [e for e in norm_errors if not is_rule_ignored(e.rule_identifier, ignored, global_ignored)]
            payload = [asdict(e) for e in filtered]
            issue_count += len(payload)
            cache_put_sentence(loc_key, sent_text, payload, ctx=self.ctx, doc_id=a_doc_id, checker_identity=ident)
            for err_item in payload:
                adj = dict(err_item)
                adj["n_error_start"] = sent_start + err_item.get("n_error_start", 0)
                combined_errors.append(adj)
        if last_text and not ensuring:
            iw = "issue" if issue_count == 1 else "issues"
            emit_grammar_status("done", last_text, result=f"{issue_count} {iw}")
        return True

    def _enqueue_misses(self, a_doc_id: str, a_text: str, loc_key: str, uncached_spans: list[tuple[int, int, str]]) -> None:
        """Enqueue uncached sentences for background processing."""
        provider = getattr(self, "_provider", "")
        for sent_start, sent_end, sent_text in uncached_spans:
            seq = next_enqueue_seq()
            complete_sentence = looks_complete_sentence(sent_text)
            inflight_key = grammar_inflight_key(a_doc_id, loc_key, sent_text, complete_sentence)
            grammar_obs("do_proofreading_enqueue", doc_id=a_doc_id, grammar_bcp47=loc_key, inflight_key=inflight_key, enqueue_seq=seq, n_start=sent_start, n_end=sent_end, slice_len=len(sent_text), partial_sentence_arg=not complete_sentence)
            emit_grammar_status("start", sent_text, result="queued")
            grammar_queue.enqueue(
                GrammarWorkItem(
                    ctx=self.ctx,
                    text=sent_text,
                    grammar_bcp47=loc_key,
                    partial_sentence=not complete_sentence,
                    doc_id=a_doc_id,
                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                    provider=provider,
                )
            )

    # --- XProofreader ---
    def isSpellChecker(self) -> bool:
        return False

    def doProofreading(self, aDocumentIdentifier: str, aText: str, aLocale: Any, nStartOfSentencePosition: int, nSuggestedBehindEndOfSentencePosition: int, aProperties: Any) -> Any:
        self._last_doc_id = aDocumentIdentifier
        from plugin.writer.locale.grammar_persistence import get_document_model_for_id

        if get_document_model_for_id(self.ctx, aDocumentIdentifier) is None:
            _run_on_main_thread(_ensure_persistence_bound, self.ctx, aDocumentIdentifier)
        if uno_mod is None:
            log.warning("[grammar] doProofreading: uno_mod is None (import failed)")
            raise RuntimeError("uno not available")

        a_res: Any = None
        try:
            a_res = _create_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)

            loc_key = self._check_enabled_and_locale(aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)
            if not loc_key:
                return a_res

            # 1. One BreakIterator + dialogue-merge pass for the whole paragraph.
            paragraph_spans = candidate_sentence_spans_for_proofreading(self.ctx, loc_key, aText, 0, len(aText))
            paragraph_spans = filter_sentence_spans_for_thresholds(paragraph_spans)

            # 2. Active window: overlap-filter those spans (n_start==0 keeps all).
            active_spans = self._resolve_work_spans(
                aDocumentIdentifier,
                loc_key,
                aText,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
                paragraph_spans,
            )
            if not active_spans:
                grammar_obs(
                    "do_proofreading_result_window",
                    doc_id=aDocumentIdentifier,
                    grammar_bcp47=loc_key,
                    n_start=nStartOfSentencePosition,
                    n_behind=getattr(a_res, "nBehindEndOfSentencePosition", None),
                    n_next=getattr(a_res, "nStartOfNextSentencePosition", None),
                    n_errors=0,
                    paragraph_spans=len(paragraph_spans),
                    active_spans=0,
                    uncached_active=0,
                    in_window=0,
                    before_window=0,
                    after_window=0,
                    straddle=0,
                    error_spans="",
                    skip="no_active_spans",
                )
                return a_res

            # We set the covered end to the end of the active spans we are checking
            covered_end = calculate_covered_span_end(active_spans)
            _apply_proofreading_end_positions(a_res, aText, covered_end)

            grammar_obs(
                "do_proofreading_covered_span",
                doc_id=aDocumentIdentifier,
                grammar_bcp47=loc_key,
                covered_end=covered_end,
                sentence_count=len(active_spans),
                n_start_lo=nStartOfSentencePosition,
                n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition,
                n_next=getattr(a_res, "nStartOfNextSentencePosition", None),
            )

            # 3. Cache lookup. Harper is synchronous: return only the sentence(s)
            #    this call asked about. LLM / queued providers still look up the
            #    whole paragraph so a later call can carry earlier sentences'
            #    now-warm cache (Writer honors aErrors outside the active window).
            is_harper = self._active_grammar_provider() == "harper"
            cache_spans = active_spans if is_harper else paragraph_spans
            combined_errors, uncached_cache_spans = self._process_cache_hits(
                aDocumentIdentifier, loc_key, cache_spans
            )

            if combined_errors:
                a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors), self.ctx, aDocumentIdentifier)

            # 4. Uncached work is only the active window. Harper already looked
            #    up active_spans; queued providers intersect paragraph misses.
            uncached_active_spans = (
                uncached_cache_spans
                if is_harper
                else reconcile_active_and_paragraph_spans(active_spans, uncached_cache_spans)
            )

            _obs_result_window(
                aDocumentIdentifier,
                loc_key,
                a_res,
                combined_errors,
                paragraph_span_count=len(paragraph_spans),
                active_span_count=len(active_spans),
                uncached_active_count=len(uncached_active_spans),
            )

            if not uncached_active_spans:
                grammar_obs("do_proofreading_cache_all_hit", doc_id=aDocumentIdentifier, grammar_bcp47=loc_key, sentence_count=len(active_spans), error_count=len(combined_errors))
                return a_res

            cached_ct = len(active_spans) - len(uncached_active_spans)
            miss_reason = "partial_miss" if cached_ct > 0 else "all_uncached"

            grammar_obs("do_proofreading_cache_partial_hit", doc_id=aDocumentIdentifier, grammar_bcp47=loc_key, cached_count=cached_ct, uncached_count=len(uncached_active_spans), errors_returned=len(combined_errors), miss_reason=miss_reason)

            if self._try_harper_fast_path(aDocumentIdentifier, loc_key, uncached_active_spans, combined_errors):
                if combined_errors:
                    a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors), self.ctx, aDocumentIdentifier)
                return a_res

            self._enqueue_misses(aDocumentIdentifier, aText, loc_key, uncached_active_spans)
            log.debug("[grammar] doProofreading: async miss returning partial or empty errors; sentence cache fills in background")
            return a_res

        except Exception as e:
            log.exception("[grammar] doProofreading failed: %s", e)
            if a_res is not None:
                return a_res

            # Absolute fallback: try to return a fresh empty result if possible
            return _create_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        try:
            del aLocale
            doc_id = getattr(self, "_last_doc_id", None)
            _run_on_main_thread(_ignore_rule_on_main, self.ctx, doc_id, aRuleIdentifier)
        except Exception as e:
            log.warning("[grammar] ignoreRule: %s", e, exc_info=True)

    def resetIgnoreRules(self) -> None:
        try:
            doc_id = getattr(self, "_last_doc_id", None)
            _run_on_main_thread(_reset_ignore_rules_on_main, self.ctx, doc_id)
        except Exception as e:
            log.warning("[grammar] resetIgnoreRules: %s", e, exc_info=True)

    def addLinguServiceEventListener(self, xLstnr: Any) -> bool:
        if xLstnr is None:
            return False
        if xLstnr not in self._lingu_listeners:
            self._lingu_listeners.append(xLstnr)
        return True

    def removeLinguServiceEventListener(self, xLstnr: Any) -> bool:
        if xLstnr is None:
            return False
        try:
            self._lingu_listeners.remove(xLstnr)
            return True
        except ValueError:
            return False

    def broadcast_proofread_again(self) -> None:
        """Ask Writer's grammar iterator to walk the document again (PROOFREAD_AGAIN)."""
        if not self._lingu_listeners:
            return
        n_event = 8  # com.sun.star.linguistic2.LinguServiceEventFlags.PROOFREAD_AGAIN
        event: Any = None
        if uno_mod is not None:
            try:
                event = uno_mod.createUnoStruct("com.sun.star.linguistic2.LinguServiceEvent")
                setattr(event, "Source", self)
                setattr(event, "nEvent", n_event)
            except Exception:
                event = None
        if event is None:
            from types import SimpleNamespace

            event = SimpleNamespace(Source=self, nEvent=n_event)
        for listener in list(self._lingu_listeners):
            try:
                listener.processLinguServiceEvent(event)
            except Exception:
                log.debug("[grammar] lingu service listener failed", exc_info=True)

    # --- XServiceDisplayName ---
    def getServiceDisplayName(self, aLocale: Any) -> str:
        from plugin.framework.i18n import _

        # Keep the product brand untranslated; only localize the role label.
        return f"WriterAgent {_('AI Grammar')}"


try:
    g_ImplementationHelper = unohelper.ImplementationHelper()
    g_ImplementationHelper.addImplementation(WriterAgentAiGrammarProofreader, IMPLEMENTATION_NAME, (SERVICE_NAME,))
except (ImportError, AttributeError):
    g_ImplementationHelper = None  # type: ignore[assignment]
