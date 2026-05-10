# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Linguistic2 grammar checker (Lightproof-style): XProofreader backed by LLM + cache."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time  # noqa: F401 — tests patch ``ai_grammar_proofreader.time.sleep``

# LO loads this file as a standalone UNO component; set up path like panel_factory.py
# so ``import plugin...`` works (file is plugin/writer/locale/ai_grammar_proofreader.py).
_this_file = os.path.abspath(__file__)
for _i in range(4):
    _this_file = os.path.dirname(_this_file)
_ext_root = _this_file
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
_lib_dir = os.path.join(_ext_root, "plugin", "lib")
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)
from typing import Any, Sequence, cast

import unohelper

from com.sun.star.lang import XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales

log = logging.getLogger("writeragent.grammar")
# Do not inherit writeragent's log level (often WARN); grammar uses INFO for diagnostics.
log.setLevel(logging.DEBUG)

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

from .grammar_proofread_cache import cache_get_sentence, ignore_rule_add, ignore_rules_clear, ignored_rules_snapshot
from .grammar_proofread_locale import (
    GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS,  # noqa: F401 — module API for tests (`proofreader.GRAMMAR_*`)
    GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS,  # noqa: F401
    GRAMMAR_REGISTRY_LOCALE_TAGS,
    bcp47_to_uno_lang_country,
    count_nonspace_chars,
    looks_complete_sentence,
    normalize_uno_locale_to_bcp47,
)
from .grammar_proofread_text import (
    NormalizedProofError,
    candidate_sentence_spans_for_proofreading,
    filter_sentence_spans_for_thresholds,
    grammar_inflight_key,
)
from .grammar_work_queue import (
    GrammarWorkItem,
    GrammarWorkQueue as _GrammarWorkQueue,  # noqa: F401 — test hook ``proofreader._GrammarWorkQueue``
    emit_grammar_status,
    grammar_obs,
    grammar_queue,
    next_enqueue_seq,
    run_llm_and_cache as _run_llm_and_cache,  # noqa: F401 — test hook ``proofreader._run_llm_and_cache``
    slice_preview_debug,
)

# Tests and legacy call sites: module-level aliases
_slice_preview_debug = slice_preview_debug
_grammar_obs = grammar_obs
_emit_grammar_status = emit_grammar_status
_count_nonspace_chars = count_nonspace_chars
_looks_complete_sentence = looks_complete_sentence
_grammar_inflight_key = grammar_inflight_key


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


def _cached_errors_to_uno_tuple(cached: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    ignored_now = ignored_rules_snapshot()
    norms = [
        NormalizedProofError(n_error_start=int(d["n_error_start"]), n_error_length=int(d["n_error_length"]), suggestions=tuple(d.get("suggestions") or ()), short_comment=str(d.get("short_comment", "")), full_comment=str(d.get("full_comment", "")), rule_identifier=str(d.get("rule_identifier", "")))
        for d in cached
        if str(d.get("rule_identifier", "")) not in ignored_now
    ]
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
    except Exception as e:
        log.error("[grammar] _locale_tuple: Locale construction failed: %s", e, exc_info=True)
        return ()


def ensure_writeragent_proofreader_configured(ctx: Any) -> None:
    """Log Doc-tab grammar state only.

    We intentionally do **not** call ``XLinguServiceManager2.setConfiguredServices`` here: doing that
    during startup/sidebar init has been observed to destabilize LibreOffice (Writing aids / proofreader
    list). The Linguistic ``GrammarCheckers`` XCU is bundled in the default OXT; users still pick the
    active grammar checker under Tools → Options → Language Settings → Writing aids.
    """
    from plugin.framework.config import get_config_bool
    from plugin.framework.logging import init_logging

    try:
        init_logging(ctx)
    except Exception as e:
        log.warning("[grammar] ensure_proofreader_selection: init_logging: %s", e, exc_info=True)
    log.info("[grammar] ensure_proofreader_selection: entry")
    if uno_mod is None:
        log.warning("[grammar] ensure_proofreader_selection: uno module missing, skipping")
        return
    try:
        enabled = get_config_bool(ctx, "doc.grammar_proofreader_enabled")
    except Exception as e:
        log.warning("[grammar] ensure_proofreader_selection: cannot read doc.grammar_proofreader_enabled: %s", e, exc_info=True)
        return
    if not enabled:
        log.info("[grammar] ensure_proofreader_selection: Doc-tab AI grammar off (enable on Doc tab to use the checker)")
        return
    log.info("[grammar] Doc-tab AI grammar on — if Writer does not underline yet, set WriterAgent as the active grammar checker under Tools → Options → Language Settings → Writing aids for the document language (same locales as the extension’s UI translation set).")


def _build_empty_result(proofreader: Any, a_document_identifier: Any, a_text: str, a_locale: Any, n_start_of_sentence_position: int, n_suggested_behind_end_of_sentence_position: int) -> Any:
    """Initialize ProofreadingResult (sentence bounds aligned with Lightproof)."""
    if uno_mod is None:
        raise RuntimeError("uno not available")
    try:
        a_res = cast("Any", uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult"))
    except Exception as e:
        log.exception("[grammar] _build_empty_result: createUnoStruct ProofreadingResult failed: %s", e)
        raise
    try:
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
        log.exception("[grammar] _build_empty_result: filling ProofreadingResult failed: %s", e)
        raise


def _apply_proofreading_end_positions(a_res: Any, a_text: str, covered_end: int) -> None:
    """Set traversal positions from the one-past-end of the span we actually checked (sentence-sized).

    Skips spaces after ``covered_end`` so Writer advances past inter-sentence whitespace.
    """
    n_next = min(max(0, covered_end), len(a_text))
    n_next = _advance_past_leading_whitespace(a_text, n_next)
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next


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


class WriterAgentAiGrammarProofreader(unohelper.Base, XProofreader, XServiceInfo, XServiceName, XServiceDisplayName, XSupportedLocales):  # pyright: ignore[reportGeneralTypeIssues] — multiple UNO interface bases  # pyrefly: ignore[invalid-inheritance]
    """Grammar checker registered under Linguistic / GrammarCheckers (cf. Lightproof)."""

    def __init__(self, ctx: Any, *args: Any):
        # LibreOffice's Linguistic manager instantiates proofreaders with
        # compatibility arguments before querying XSupportedLocales.
        del args
        super().__init__()
        self.ctx = ctx
        self._implementation_name = IMPLEMENTATION_NAME
        self._supported_service_names = (SERVICE_NAME,)
        try:
            self._locales = _locale_tuple()
        except Exception as e:
            log.error("[grammar] WriterAgentAiGrammarProofreader.__init__: _locale_tuple failed: %s", e, exc_info=True)
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
    def hasLocale(self, aLocale: Any) -> bool:
        try:
            if aLocale is None or not self._locales:
                return False
            return normalize_uno_locale_to_bcp47(aLocale) is not None
        except Exception as e:
            log.warning("[grammar] hasLocale: %s", e, exc_info=True)
            return False

    def getLocales(self) -> tuple[Any, ...]:
        try:
            return self._locales
        except Exception as e:
            log.warning("[grammar] getLocales: %s", e, exc_info=True)
            return ()

    # --- XProofreader ---
    def isSpellChecker(self) -> bool:
        return False

    def doProofreading(self, aDocumentIdentifier: str, aText: str, aLocale: Any, nStartOfSentencePosition: int, nSuggestedBehindEndOfSentencePosition: int, aProperties: Any) -> Any:
        if uno_mod is None:
            log.warning("[grammar] doProofreading: uno_mod is None (import failed)")
            raise RuntimeError("uno not available")
        a_res: Any = None
        try:
            from plugin.framework.config import get_config_bool
            from plugin.framework.logging import init_logging
            try:
                init_logging(self.ctx)
            except Exception as e:
                log.warning("[grammar] doProofreading: init_logging: %s", e, exc_info=True)
            a_res = _build_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)
            try:
                enabled = get_config_bool(self.ctx, "doc.grammar_proofreader_enabled")
            except Exception as e:
                log.warning("[grammar] doProofreading: could not read doc.grammar_proofreader_enabled -> off: %s", e, exc_info=True)
                enabled = False
            loc_raw = _locale_key(aLocale)
            grammar_bcp47 = normalize_uno_locale_to_bcp47(aLocale)
            if not enabled:
                global _GRAMMAR_DISABLED_NOTICE_EMITTED
                if not _GRAMMAR_DISABLED_NOTICE_EMITTED:
                    _GRAMMAR_DISABLED_NOTICE_EMITTED = True
                    log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
                _grammar_obs("do_proofreading_skip", reason="grammar_disabled", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, locale_raw=loc_raw)
                return a_res
            _GRAMMAR_DISABLED_NOTICE_EMITTED = False
            if grammar_bcp47 is None:
                log.info("[grammar] doProofreading: locale not in WriterAgent registry: %s", loc_raw)
                _grammar_obs("do_proofreading_skip", reason="locale_not_registered", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, locale_raw=loc_raw)
                return a_res
            loc_key = grammar_bcp47
            _grammar_obs("do_proofreading_entry", doc_id=aDocumentIdentifier, len_aText=len(aText), n_start_lo=nStartOfSentencePosition, n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition, grammar_bcp47=grammar_bcp47, locale_raw=loc_raw, text_preview=_slice_preview_debug(aText))

            raw_spans = candidate_sentence_spans_for_proofreading(self.ctx, loc_key, aText, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)
            work_spans = filter_sentence_spans_for_thresholds(raw_spans)
            if not work_spans:
                log.info("[grammar] doProofreading: no eligible sentences (overlap/threshold) n_start=%s", nStartOfSentencePosition)
                _grammar_obs(
                    "do_proofreading_skip",
                    reason="no_eligible_sentences_or_incomplete_short",
                    doc_id=aDocumentIdentifier,
                    n_start_lo=nStartOfSentencePosition,
                    raw_candidates=len(raw_spans),
                    grammar_bcp47=grammar_bcp47,
                )
                return a_res

            covered_end = max(end for _s, end, _t in work_spans)
            _apply_proofreading_end_positions(a_res, aText, covered_end)
            _grammar_obs(
                "do_proofreading_covered_span",
                doc_id=aDocumentIdentifier,
                grammar_bcp47=grammar_bcp47,
                covered_end=covered_end,
                sentence_count=len(work_spans),
                n_start_lo=nStartOfSentencePosition,
                n_suggested_behind_end=nSuggestedBehindEndOfSentencePosition,
                n_next=getattr(a_res, "nStartOfNextSentencePosition", None),
            )
            log.info(
                "[grammar] doProofreading doc_id=%r len_text=%s locale=%s lo_range=[%s,%s) covered_end=%s sentences=%s enabled=%s",
                aDocumentIdentifier,
                len(aText),
                loc_key,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
                covered_end,
                len(work_spans),
                enabled,
            )

            combined_errors: list[dict[str, Any]] = []
            uncached_spans: list[tuple[int, int, str]] = []
            for sent_start, _sent_end, sent_text in work_spans:
                cached = cache_get_sentence(loc_key, sent_text)
                _grammar_obs(
                    "do_proofreading_sentence_cache",
                    doc_id=aDocumentIdentifier,
                    sent_start=sent_start,
                    sent_len=len(sent_text),
                    cache_hit=cached is not None,
                    sent_preview=_slice_preview_debug(sent_text, 48),
                )
                if cached is None:
                    uncached_spans.append((sent_start, _sent_end, sent_text))
                    continue
                for err_item in cached:
                    adj = dict(err_item)
                    adj["n_error_start"] = sent_start + err_item.get("n_error_start", 0)
                    combined_errors.append(adj)

            if not uncached_spans:
                try:
                    a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                    log.info("[grammar] per-sentence cache ALL HIT: %s sentence(s), %s error(s)", len(work_spans), len(combined_errors))
                    _grammar_obs("do_proofreading_cache_all_hit", doc_id=aDocumentIdentifier, grammar_bcp47=grammar_bcp47, sentence_count=len(work_spans), error_count=len(combined_errors))
                except Exception as e:
                    log.exception("[grammar] doProofreading: per-sentence cache HIT path failed: %s", e)
                    try:
                        a_res.aErrors = ()
                    except Exception:
                        pass
                return a_res

            if combined_errors:
                try:
                    a_res.aErrors = _cached_errors_to_uno_tuple(tuple(combined_errors))
                except Exception as e:
                    log.exception("[grammar] doProofreading: partial cache path failed: %s", e)
            cached_ct = len(work_spans) - len(uncached_spans)
            if cached_ct > 0:
                log.info(
                    "[grammar] per-sentence cache PARTIAL HIT: %s cached, %s uncached → enqueueing sentence-sized item(s)",
                    cached_ct,
                    len(uncached_spans),
                )
                miss_reason = "partial_miss"
            else:
                log.info(
                    "[grammar] per-sentence cache MISS (all %s sentence(s) uncached) → enqueueing",
                    len(uncached_spans),
                )
                miss_reason = "all_uncached"
            _grammar_obs(
                "do_proofreading_cache_partial_hit",
                doc_id=aDocumentIdentifier,
                grammar_bcp47=grammar_bcp47,
                cached_count=cached_ct,
                uncached_count=len(uncached_spans),
                errors_returned=len(combined_errors),
                miss_reason=miss_reason,
            )

            for sent_start, sent_end, sent_text in uncached_spans:
                seq = next_enqueue_seq()
                inflight_key = _grammar_inflight_key(aDocumentIdentifier, loc_key, sent_start)
                complete_sentence = _looks_complete_sentence(sent_text)
                log.info("[grammar] cache MISS enqueue sentence seq=%s key=%s len=%s", seq, inflight_key, len(sent_text))
                _grammar_obs(
                    "do_proofreading_enqueue",
                    doc_id=aDocumentIdentifier,
                    grammar_bcp47=grammar_bcp47,
                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                    n_start=sent_start,
                    n_end=sent_end,
                    slice_len=len(sent_text),
                    partial_sentence_arg=not complete_sentence,
                )
                _emit_grammar_status("start", sent_text, result="queued")
                grammar_queue.enqueue(
                    GrammarWorkItem(
                        ctx=self.ctx,
                        full_text=aText,
                        n_start=sent_start,
                        n_end=sent_end,
                        grammar_bcp47=grammar_bcp47,
                        partial_sentence=not complete_sentence,
                        doc_id=aDocumentIdentifier,
                        inflight_key=inflight_key,
                        enqueue_seq=seq,
                        proofread_sentence_text=sent_text,
                    )
                )
            log.info("[grammar] doProofreading: async miss returning partial or empty errors; sentence cache fills in background")
            return a_res
        except Exception as e:
            log.exception("[grammar] doProofreading failed (returning empty errors if possible): %s", e)
            if a_res is not None:
                try:
                    a_res.aErrors = ()
                except Exception:
                    pass
                return a_res
            try:
                from plugin.framework.logging import init_logging

                init_logging(self.ctx)
            except Exception:
                pass
            return _build_empty_result(self, aDocumentIdentifier, aText, aLocale, nStartOfSentencePosition, nSuggestedBehindEndOfSentencePosition)

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        try:
            del aLocale  # locale-specific ignore not distinguished in cache yet
            ignore_rule_add(str(aRuleIdentifier))
        except Exception as e:
            log.warning("[grammar] ignoreRule: %s", e, exc_info=True)

    def resetIgnoreRules(self) -> None:
        try:
            ignore_rules_clear()
        except Exception as e:
            log.warning("[grammar] resetIgnoreRules: %s", e, exc_info=True)

    # --- XServiceDisplayName ---
    def getServiceDisplayName(self, aLocale: Any) -> str:
        try:
            _ = aLocale
            return "WriterAgent AI Grammar"
        except Exception as e:
            log.warning("[grammar] getServiceDisplayName: %s", e, exc_info=True)
            return "WriterAgent AI Grammar"


try:
    import unohelper

    g_ImplementationHelper = unohelper.ImplementationHelper()
    g_ImplementationHelper.addImplementation(WriterAgentAiGrammarProofreader, IMPLEMENTATION_NAME, (SERVICE_NAME,))
except (ImportError, AttributeError):
    g_ImplementationHelper = None  # type: ignore[assignment]
