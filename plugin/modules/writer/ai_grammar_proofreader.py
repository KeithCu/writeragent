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

# LO loads this file as a standalone UNO component; set up path like panel_factory.py
# so ``import plugin...`` works (file is plugin/modules/writer/ai_grammar_proofreader.py).
_this_file = os.path.abspath(__file__)
for _i in range(4):
    _this_file = os.path.dirname(_this_file)
_ext_root = _this_file
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
_lib_dir = os.path.join(_ext_root, "plugin", "lib")
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)
import threading
import time
from dataclasses import asdict
from typing import Any, no_type_check

import unohelper

from com.sun.star.lang import XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XLinguServiceManager2, XProofreader, XSupportedLocales

from plugin.framework.config import (
    get_api_config,
    get_config_bool,
    get_config_int,
    get_config_str,
    get_text_model,
)
from plugin.framework.logging import init_logging
from plugin.framework.worker_pool import run_in_background
from plugin.modules.http.client import LlmClient
from plugin.modules.writer import grammar_proofread_engine as _engine

log = logging.getLogger("writeragent.grammar")
# Do not inherit writeragent's log level (often WARN); grammar uses INFO for diagnostics.
log.setLevel(logging.DEBUG)

IMPLEMENTATION_NAME = "org.extension.writeragent.comp.pyuno.AiGrammarProofreader"
SERVICE_NAME = "com.sun.star.linguistic2.Proofreader"

uno_mod: Any
try:
    uno_mod = importlib.import_module("uno")
except ImportError:
    uno_mod = None

_DEBOUNCE_SEQ: dict[str, int] = {}


@no_type_check
def _proofreading_markup_type() -> int:
    """``com.sun.star.text.TextMarkupType.PROOFREADING`` via PyUNO constant lookup."""
    if uno_mod is None:
        return 0
    try:
        v: Any = uno_mod.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")
        return int(v)
    except Exception:
        return 4


_DEBOUNCE_LOCK = threading.Lock()


def _locale_key(loc: Any) -> str:
    try:
        return f"{loc.Language}_{loc.Country}_{loc.Variant}"
    except Exception:
        return "unknown"


@no_type_check
def _locale_tuple() -> tuple[Any, ...]:
    if uno_mod is None:
        return ()
    return (
        uno_mod.createUnoStruct("com.sun.star.lang.Locale", "en", "US", ""),
        uno_mod.createUnoStruct("com.sun.star.lang.Locale", "en", "GB", ""),
        uno_mod.createUnoStruct("com.sun.star.lang.Locale", "en", "", ""),
    )


@no_type_check
def _configured_proofreader_tuple(cur: Any) -> tuple[str, ...]:
    if cur is None:
        return ()
    try:
        return tuple(str(x) for x in cur)
    except Exception:
        s = str(cur).strip()
        return (s,) if s else ()


@no_type_check
def ensure_writeragent_proofreader_configured(ctx: Any) -> None:
    """If Doc-tab AI grammar is enabled, select this UNO impl as LO's active Proofreader for English.

    Registry XCU only registers the checker; Writer still calls whichever proofreader is
    configured for the locale (Lightproof, LanguageTool, …). This applies ``setConfiguredServices``.
    """
    if uno_mod is None:
        return
    try:
        init_logging(ctx)
    except Exception:
        pass
    try:
        if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
            return
    except Exception:
        return
    try:
        sm = ctx.getServiceManager()
    except Exception:
        try:
            sm = ctx.ServiceManager
        except Exception:
            return
    try:
        lingo = sm.createInstanceWithContext("com.sun.star.linguistic2.LinguServiceManager", ctx)
    except Exception as e:
        log.warning("[grammar] ensure configured: LinguServiceManager: %s", e)
        return
    lingo2 = uno_mod.QueryInterface(XLinguServiceManager2, lingo)
    if not lingo2:
        log.warning("[grammar] ensure configured: XLinguServiceManager2 not available")
        return
    svc = SERVICE_NAME
    want = (IMPLEMENTATION_NAME,)
    for loc in _locale_tuple():
        key = _locale_key(loc)
        try:
            cur = lingo2.getConfiguredServices(svc, loc)
        except Exception as e:
            log.warning("[grammar] getConfiguredServices failed for locale %s: %s", key, e)
            continue
        if _configured_proofreader_tuple(cur) == want:
            continue
        try:
            lingo2.setConfiguredServices(svc, loc, want)
            log.info(
                "[grammar] set LibreOffice active Proofreader for locale %s to %s (was %s)",
                key,
                IMPLEMENTATION_NAME,
                _configured_proofreader_tuple(cur),
            )
        except Exception as e:
            log.warning("[grammar] setConfiguredServices failed for locale %s: %s", key, e)


@no_type_check
def _build_empty_result(
    proofreader: Any,
    a_document_identifier: Any,
    a_text: str,
    a_locale: Any,
    n_start_of_sentence_position: int,
    n_suggested_behind_end_of_sentence_position: int,
) -> Any:
    """Initialize ProofreadingResult (sentence bounds aligned with Lightproof)."""
    a_res: Any = uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult")
    a_res.aDocumentIdentifier = a_document_identifier
    a_res.aText = a_text
    a_res.aLocale = a_locale
    a_res.nStartOfSentencePosition = n_start_of_sentence_position
    a_res.nStartOfNextSentencePosition = n_suggested_behind_end_of_sentence_position
    a_res.aProperties = ()
    a_res.xProofreader = proofreader
    a_res.aErrors = ()
    n_next = n_suggested_behind_end_of_sentence_position
    if n_next < len(a_text):
        ch = a_text[n_next : n_next + 1]
        while ch == " ":
            n_next += 1
            ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
        if n_next == n_suggested_behind_end_of_sentence_position and ch != "":
            n_next = n_suggested_behind_end_of_sentence_position + 1
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next
    return a_res


@no_type_check
def _errors_to_uno_tuple(
    norms: list[_engine.NormalizedProofError],
) -> tuple[Any, ...]:
    out: list[Any] = []
    for e in norms:
        a_err: Any = uno_mod.createUnoStruct("com.sun.star.linguistic2.SingleProofreadingError")
        a_err.nErrorStart = e.n_error_start
        a_err.nErrorLength = e.n_error_length
        a_err.nErrorType = _proofreading_markup_type()
        a_err.aRuleIdentifier = e.rule_identifier
        a_err.aSuggestions = tuple(e.suggestions)
        a_err.aShortComment = e.short_comment
        a_err.aFullComment = e.full_comment
        a_err.aProperties = ()
        out.append(a_err)
    return tuple(out)


def _run_llm_and_cache(
    ctx: Any,
    cache_key: str,
    fingerprint: str,
    full_text: str,
    n_start: int,
    n_end: int,
    debounce_seq: int,
    map_key: str,
) -> None:
    try:
        debounce_ms = get_config_int(ctx, "doc.grammar_proofreader_debounce_ms")
        log.debug(
            "[grammar] worker sleep debounce_ms=%s key=%s seq=%s",
            debounce_ms,
            map_key[:80] if len(map_key) > 80 else map_key,
            debounce_seq,
        )
        time.sleep(debounce_ms / 1000.0)
        with _DEBOUNCE_LOCK:
            cur = _DEBOUNCE_SEQ.get(map_key, -1)
        if cur != debounce_seq:
            log.info(
                "[grammar] worker superseded (debounce): map_key=%s had_seq=%s want_seq=%s",
                map_key[:120],
                cur,
                debounce_seq,
            )
            return
        if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
            log.info("[grammar] worker skipped: doc.grammar_proofreader_enabled is false after debounce")
            return
        max_chars = get_config_int(ctx, "doc.grammar_proofreader_max_chars")
        slice_txt = full_text[n_start:n_end]
        if len(slice_txt) > max_chars:
            log.info(
                "[grammar] worker skipped: slice len %s > max_chars %s",
                len(slice_txt),
                max_chars,
            )
            return
        max_tok = get_config_int(ctx, "doc.grammar_proofreader_max_tokens")
        model = get_config_str(ctx, "doc.grammar_proofreader_model").strip() or get_text_model(ctx)
        sys_prompt = (
            "You are a strict grammar and style checker. Reply with a single JSON object only, "
            'no markdown, shaped exactly as: {"errors": [{"wrong": "exact substring from the text", '
            '"correct": "replacement", "type": "grammar|style|spelling", "reason": "brief reason"}]}. '
            "Use an empty errors array if there are no issues."
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": slice_txt},
        ]
        log.info(
            "[grammar] LLM request slice_len=%s max_tokens=%s model=%s",
            len(slice_txt),
            max_tok,
            model or "(default text model)",
        )
        client = LlmClient(get_api_config(ctx), ctx)
        content = client.chat_completion_sync(messages, max_tokens=max_tok, model=model or None)
        log.debug("[grammar] LLM raw response length=%s", len(content or ""))
        items = _engine.parse_grammar_json(content or "")
        log.info("[grammar] parsed %s error item(s) from JSON", len(items))
        ignored = _engine.ignored_rules_snapshot()
        norms = _engine.normalize_errors_for_text(full_text, n_start, n_end, items, ignored)
        _engine.cache_put(cache_key, fingerprint, [asdict(n) for n in norms])
        log.info("[grammar] cached %s normalized error(s) for key fp=%s…", len(norms), fingerprint[:12])
    except Exception as e:
        log.warning("[grammar] worker failed: %s", e, exc_info=True)


@no_type_check
class WriterAgentAiGrammarProofreader(
    unohelper.Base,
    XProofreader,
    XSupportedLocales,
    XServiceInfo,
    XServiceName,
    XServiceDisplayName,
):
    """Grammar checker registered under Linguistic / GrammarCheckers (cf. Lightproof)."""

    def __init__(self, ctx: Any):
        super().__init__()
        self.ctx = ctx
        self._implementation_name = IMPLEMENTATION_NAME
        self._supported_service_names = (SERVICE_NAME,)
        self._locales = _locale_tuple()

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
        if not self._locales:
            return False
        for i in self._locales:
            try:
                if i == aLocale:
                    return True
                if i.Language == aLocale.Language and (
                    i.Country == aLocale.Country or i.Country == "" or aLocale.Country == ""
                ):
                    return True
            except Exception:
                continue
        return False

    def getLocales(self) -> tuple[Any, ...]:
        return self._locales

    # --- XProofreader ---
    def isSpellChecker(self) -> bool:
        return False

    def doProofreading(
        self,
        aDocumentIdentifier: str,
        aText: str,
        aLocale: Any,
        nStartOfSentencePosition: int,
        nSuggestedBehindEndOfSentencePosition: int,
        aProperties: Any,
    ) -> Any:
        if uno_mod is None:
            log.warning("[grammar] doProofreading: uno_mod is None (import failed)")
            raise RuntimeError("uno not available")
        try:
            init_logging(self.ctx)
        except Exception as e:
            log.debug("[grammar] init_logging: %s", e)
        a_res = _build_empty_result(
            self,
            aDocumentIdentifier,
            aText,
            aLocale,
            nStartOfSentencePosition,
            nSuggestedBehindEndOfSentencePosition,
        )
        try:
            enabled = get_config_bool(self.ctx, "doc.grammar_proofreader_enabled")
        except Exception as e:
            log.info("[grammar] doProofreading: could not read doc.grammar_proofreader_enabled (%s) -> off", e)
            enabled = False
        loc_key = _locale_key(aLocale)
        log.info(
            "[grammar] doProofreading doc_id=%r len_text=%s locale=%s range=[%s,%s) enabled=%s",
            aDocumentIdentifier,
            len(aText),
            loc_key,
            nStartOfSentencePosition,
            nSuggestedBehindEndOfSentencePosition,
            enabled,
        )
        if not enabled:
            log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
            return a_res
        if not self.hasLocale(aLocale):
            log.info("[grammar] doProofreading: locale not supported (have en_US/en_GB/en): %s", loc_key)
            return a_res
        n_start = max(0, nStartOfSentencePosition)
        n_end = min(len(aText), nSuggestedBehindEndOfSentencePosition)
        if n_end <= n_start:
            log.info("[grammar] doProofreading: empty span after clamp (%s,%s)", n_start, n_end)
            return a_res
        slice_txt = aText[n_start:n_end]
        try:
            max_chars = get_config_int(self.ctx, "doc.grammar_proofreader_max_chars")
        except Exception:
            max_chars = 8000
        if len(slice_txt) > max_chars:
            log.info(
                "[grammar] doProofreading: slice too long (%s chars, max %s) — skipping LLM",
                len(slice_txt),
                max_chars,
            )
            return a_res
        cache_key = _engine.make_cache_key(aDocumentIdentifier, n_start, n_end, loc_key)
        fp = _engine.fingerprint_for_text(slice_txt)
        cached = _engine.cache_get(cache_key, fp)
        if cached is not None:
            ignored_now = _engine.ignored_rules_snapshot()
            norms = [
                _engine.NormalizedProofError(
                    n_error_start=int(d["n_error_start"]),
                    n_error_length=int(d["n_error_length"]),
                    suggestions=tuple(d.get("suggestions") or ()),
                    short_comment=str(d.get("short_comment", "")),
                    full_comment=str(d.get("full_comment", "")),
                    rule_identifier=str(d.get("rule_identifier", "")),
                )
                for d in cached
                if str(d.get("rule_identifier", "")) not in ignored_now
            ]
            a_res.aErrors = _errors_to_uno_tuple(norms)
            log.info("[grammar] cache HIT returning %s error(s) key=%s…", len(norms), cache_key[:80])
            return a_res

        map_key = cache_key
        with _DEBOUNCE_LOCK:
            _DEBOUNCE_SEQ[map_key] = _DEBOUNCE_SEQ.get(map_key, 0) + 1
            seq = _DEBOUNCE_SEQ[map_key]
        log.info(
            "[grammar] cache MISS scheduling worker seq=%s slice_len=%s fp=%s…",
            seq,
            len(slice_txt),
            fp[:12],
        )
        run_in_background(
            _run_llm_and_cache,
            self.ctx,
            cache_key,
            fp,
            aText,
            n_start,
            n_end,
            seq,
            map_key,
            name="writeragent-grammar-proofread",
        )
        return a_res

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        del aLocale  # locale-specific ignore not distinguished in cache yet
        _engine.ignore_rule_add(str(aRuleIdentifier))

    def resetIgnoreRules(self) -> None:
        _engine.ignore_rules_clear()

    # --- XServiceDisplayName ---
    def getServiceDisplayName(self, aLocale: Any) -> str:
        del aLocale
        return "WriterAgent AI Grammar"


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    WriterAgentAiGrammarProofreader,
    IMPLEMENTATION_NAME,
    (SERVICE_NAME,),
)
