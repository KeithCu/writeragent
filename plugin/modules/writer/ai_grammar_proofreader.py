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

from com.sun.star.lang import Locale, XServiceDisplayName, XServiceInfo, XServiceName
from com.sun.star.linguistic2 import XProofreader, XSupportedLocales

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
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_JOBS: dict[str, "_InflightGrammarJob"] = {}


@no_type_check
def _proofreading_markup_type() -> int:
    """``com.sun.star.text.TextMarkupType.PROOFREADING`` via PyUNO constant lookup."""
    if uno_mod is None:
        return 0
    try:
        v: Any = uno_mod.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")
        return int(v)
    except Exception as e:
        log.warning(
            "[grammar] _proofreading_markup_type: falling back to 4: %s",
            e,
            exc_info=True,
        )
        return 4


_DEBOUNCE_LOCK = threading.Lock()


class _InflightGrammarJob:
    def __init__(self) -> None:
        self.done = threading.Event()


def _grammar_text_preview(text: str) -> str:
    words = text.strip().split()
    return " ".join(words[:3]) if words else "(empty)"


def _emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
) -> None:
    try:
        from plugin.framework.event_bus import global_event_bus

        global_event_bus.emit(
            "grammar:status",
            phase=phase,
            preview=_grammar_text_preview(text),
            length=len(text),
            result=result,
            elapsed_ms=elapsed_ms,
        )
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


def _wait_for_inflight_job(ctx: Any, job: _InflightGrammarJob, timeout_ms: int) -> bool:
    """Wait for a grammar worker while letting LibreOffice service pending UI events."""
    timeout_s = max(0, timeout_ms) / 1000.0
    if timeout_s <= 0:
        return job.done.is_set()
    deadline = time.monotonic() + timeout_s
    try:
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(ctx)
    except Exception as e:
        log.warning("[grammar] wait: toolkit unavailable, waiting without event pump: %s", e, exc_info=True)
        toolkit = None
    while not job.done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return job.done.is_set()
        if job.done.wait(min(0.1, remaining)):
            return True
        if toolkit is not None:
            try:
                toolkit.processEventsToIdle()
            except Exception as e:
                log.warning("[grammar] wait: processEventsToIdle failed: %s", e, exc_info=True)
                toolkit = None
    return True


def _cached_errors_to_uno_tuple(cached: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    from plugin.modules.writer import grammar_proofread_engine as engine

    ignored_now = engine.ignored_rules_snapshot()
    norms = [
        engine.NormalizedProofError(
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
    return _errors_to_uno_tuple(norms)


def _locale_key(loc: Any) -> str:
    try:
        return f"{loc.Language}_{loc.Country}_{loc.Variant}"
    except Exception as e:
        log.debug("[grammar] _locale_key: %s", e, exc_info=True)
        return "unknown"


@no_type_check
def _locale_tuple() -> tuple[Any, ...]:
    """Locales returned by ``getLocales`` — must match ``LinguisticWriterAgentGrammar.xcu`` ``Locales``.

    The XCU uses hyphenated BCP47-like tags in one ``oor:string-list`` value (e.g. ``en-US en-GB``);
    UNO uses ``com.sun.star.lang.Locale`` structs for the same languages.

    LibreOffice merges the registry list with ``XSupportedLocales``; an extra locale here that is
    not listed under GrammarCheckers in the XCU has been observed to trigger a UNO RuntimeException
    when opening Tools → Options → Language Settings (Writing aids).
    """
    try:
        return (
            Locale("en", "US", ""),
            Locale("en", "GB", ""),
        )
    except Exception as e:
        log.error("[grammar] _locale_tuple: Locale construction failed: %s", e, exc_info=True)
        return ()


@no_type_check
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
        log.warning(
            "[grammar] ensure_proofreader_selection: cannot read doc.grammar_proofreader_enabled: %s",
            e,
            exc_info=True,
        )
        return
    if not enabled:
        log.info(
            "[grammar] ensure_proofreader_selection: Doc-tab AI grammar off (enable on Doc tab to use the checker)"
        )
        return
    log.info(
        "[grammar] Doc-tab AI grammar on — if Writer does not underline yet, set WriterAgent as the "
        "active grammar checker under Tools → Options → Language Settings → Writing aids for English."
    )


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
    try:
        a_res: Any = uno_mod.createUnoStruct("com.sun.star.linguistic2.ProofreadingResult")
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
        # FIXME: Adoption of lightproof batching (len(a_text)) caused missing underlines.
        # Reverted to LO suggested bounds for now.
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
    except Exception as e:
        log.exception("[grammar] _build_empty_result: filling ProofreadingResult failed: %s", e)
        raise


@no_type_check
def _errors_to_uno_tuple(
    norms: list[Any],
) -> tuple[Any, ...]:
    out: list[Any] = []
    for idx, e in enumerate(norms):
        try:
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
        except Exception as ex:
            log.warning(
                "[grammar] _errors_to_uno_tuple: skipped error index=%s rule=%r: %s",
                idx,
                getattr(e, "rule_identifier", ""),
                ex,
                exc_info=True,
            )
    return tuple(out)


def _grammar_worker_error_callback(err: Any) -> None:
    """Log worker-pool wrapper failures for grammar tasks (original exc is in details)."""
    try:
        details = getattr(err, "details", None) or {}
        log.warning(
            "[grammar] worker_pool task failed: %s details=%s",
            err,
            details,
            exc_info=True,
        )
    except Exception as e:
        log.warning("[grammar] worker_pool error_callback logging failed: %s", e, exc_info=True)


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
        from plugin.framework.config import (
            get_api_config,
            get_config_bool,
            get_config_int,
            get_config_str,
            get_text_model,
        )
        from plugin.framework.llm_concurrency import (
            is_agent_active,
            llm_request_lane,
        )
        from plugin.modules.writer import grammar_proofread_engine as engine

        try:
            debounce_ms = get_config_int(ctx, "doc.grammar_proofreader_debounce_ms")
        except Exception as e:
            log.warning("[grammar] worker: get_config_int debounce_ms: %s", e, exc_info=True)
            debounce_ms = 800
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
        try:
            if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
                log.info("[grammar] worker skipped: doc.grammar_proofreader_enabled is false after debounce")
                return
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool enabled: %s", e, exc_info=True)
            return
        try:
            pause_during_agent = get_config_bool(
                ctx, "doc.grammar_proofreader_pause_during_agent"
            )
        except Exception as e:
            log.warning(
                "[grammar] worker: get_config_bool pause_during_agent: %s",
                e,
                exc_info=True,
            )
            pause_during_agent = False
        if pause_during_agent and is_agent_active():
            log.info(
                "[grammar] worker skipped: agent active and pause_during_agent enabled"
            )
            return
        try:
            max_chars = get_config_int(ctx, "doc.grammar_proofreader_max_chars")
        except Exception as e:
            log.warning("[grammar] worker: get_config_int max_chars: %s", e, exc_info=True)
            max_chars = 8000
        slice_txt = full_text[n_start:n_end]
        if len(slice_txt) > max_chars:
            log.info(
                "[grammar] worker skipped: slice len %s > max_chars %s",
                len(slice_txt),
                max_chars,
            )
            _emit_grammar_status("skipped", slice_txt, result="too long")
            return
        try:
            max_tok = get_config_int(ctx, "doc.grammar_proofreader_max_tokens")
        except Exception as e:
            log.warning("[grammar] worker: get_config_int max_tokens: %s", e, exc_info=True)
            max_tok = 512
        try:
            model = get_config_str(ctx, "doc.grammar_proofreader_model").strip() or get_text_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""
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
        request_start = time.monotonic()
        _emit_grammar_status("request", slice_txt, result="LLM request")
        from plugin.modules.http.client import LlmClient

        client = LlmClient(get_api_config(ctx), ctx)
        with llm_request_lane():
            content = client.chat_completion_sync(
                messages,
                max_tokens=max_tok,
                model=model or None,
                response_format={"type": "json_object"},
            )
        elapsed_ms = int((time.monotonic() - request_start) * 1000)
        log.debug("[grammar] LLM raw response length=%s", len(content or ""))
        items = engine.parse_grammar_json(content or "")
        log.info("[grammar] parsed %s error item(s) from JSON", len(items))
        ignored = engine.ignored_rules_snapshot()
        norms = engine.normalize_errors_for_text(full_text, n_start, n_end, items, ignored)
        engine.cache_put(cache_key, fingerprint, [asdict(n) for n in norms])
        issue_word = "issue" if len(norms) == 1 else "issues"
        _emit_grammar_status("complete", slice_txt, result=f"{len(norms)} {issue_word}", elapsed_ms=elapsed_ms)
        log.info("[grammar] cached %s normalized error(s) for key fp=%s…", len(norms), fingerprint[:12])
    except Exception as e:
        log.error("[grammar] worker failed after config/debounce stage: %s", e, exc_info=True)
        try:
            _emit_grammar_status("failed", full_text[n_start:n_end], result=type(e).__name__)
        except Exception:
            pass


@no_type_check
class WriterAgentAiGrammarProofreader(
    unohelper.Base,
    XProofreader,
    XServiceInfo,
    XServiceName,
    XServiceDisplayName,
    XSupportedLocales,
):
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
            for i in self._locales:
                try:
                    if i == aLocale:
                        return True
                    if i.Language == aLocale.Language and (
                        i.Country == aLocale.Country or i.Country == "" or aLocale.Country == ""
                    ):
                        return True
                except Exception as ie:
                    log.debug("[grammar] hasLocale inner compare: %s", ie, exc_info=True)
                    continue
            return False
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
        a_res: Any = None
        try:
            from plugin.framework.config import get_config_bool, get_config_int
            from plugin.framework.logging import init_logging
            from plugin.framework.worker_pool import run_in_background
            from plugin.modules.writer import grammar_proofread_engine as engine

            try:
                init_logging(self.ctx)
            except Exception as e:
                log.warning("[grammar] doProofreading: init_logging: %s", e, exc_info=True)
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
                log.warning(
                    "[grammar] doProofreading: could not read doc.grammar_proofreader_enabled -> off: %s",
                    e,
                    exc_info=True,
                )
                enabled = False
            loc_key = _locale_key(aLocale)
            if not enabled:
                log.info("[grammar] doProofreading: disabled (Doc tab → Enable AI grammar checker)")
                return a_res
            if not self.hasLocale(aLocale):
                log.info(
                    "[grammar] doProofreading: locale not supported (have en-US/en-GB): %s",
                    loc_key,
                )
                return a_res
            # Lightproof.py "PATCH FOR LO 4": Writer issues incremental calls with nStart != 0; return
            # empty until the sentence-start pass. Otherwise we spam LLM/cache on every sub-span and
            # fight Writer's real grammar pass (same pattern as lightproof/Lightproof.py).
            if nStartOfSentencePosition != 0:
                log.debug(
                    "[grammar] doProofreading: skip incremental nStart=%s (await nStart==0 pass)",
                    nStartOfSentencePosition,
                )
                return a_res
            log.info(
                "[grammar] doProofreading doc_id=%r len_text=%s locale=%s range=[%s,%s) enabled=%s",
                aDocumentIdentifier,
                len(aText),
                loc_key,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
                enabled,
            )
            # FIXME: Paragraph-level batching (0, len(aText)) caused issues.
            # Reverting to incremental bounds [nStart, nSuggestedEnd).
            n_start = max(0, nStartOfSentencePosition)
            n_end = min(len(aText), nSuggestedBehindEndOfSentencePosition)
            if n_end <= n_start:
                log.info("[grammar] doProofreading: empty span after clamp (%s,%s)", n_start, n_end)
                return a_res
            slice_txt = aText[n_start:n_end]
            try:
                max_chars = get_config_int(self.ctx, "doc.grammar_proofreader_max_chars")
            except Exception as e:
                log.warning("[grammar] doProofreading: get_config_int max_chars: %s", e, exc_info=True)
                max_chars = 8000
            if len(slice_txt) > max_chars:
                log.info(
                    "[grammar] doProofreading: slice too long (%s chars, max %s) — skipping LLM",
                    len(slice_txt),
                    max_chars,
                )
                return a_res
            fp = engine.fingerprint_for_text(slice_txt)
            cache_key = engine.make_cache_key(
                aDocumentIdentifier,
                loc_key,
                fingerprint=fp,
                slice_start=n_start,
                slice_end=n_end,
            )
            cached = engine.cache_get(cache_key, fp)
            if cached is not None:
                try:
                    a_res.aErrors = _cached_errors_to_uno_tuple(cached)
                    log.info(
                        "[grammar] cache HIT returning %s error(s) key=%s…",
                        len(tuple(a_res.aErrors)),
                        cache_key[:80],
                    )
                except Exception as e:
                    log.exception("[grammar] doProofreading: cache HIT path failed: %s", e)
                    try:
                        a_res.aErrors = ()
                    except Exception:
                        pass
                return a_res

            map_key = engine.make_cache_key(aDocumentIdentifier, loc_key)
            inflight_key = cache_key
            start_worker = False
            with _INFLIGHT_LOCK:
                job = _INFLIGHT_JOBS.get(inflight_key)
                if job is None:
                    job = _InflightGrammarJob()
                    _INFLIGHT_JOBS[inflight_key] = job
                    start_worker = True
            if start_worker:
                with _DEBOUNCE_LOCK:
                    _DEBOUNCE_SEQ[map_key] = _DEBOUNCE_SEQ.get(map_key, 0) + 1
                    seq = _DEBOUNCE_SEQ[map_key]
                log.info(
                    "[grammar] cache MISS scheduling worker seq=%s slice_len=%s fp=%s…",
                    seq,
                    len(slice_txt),
                    fp[:12],
                )
                _emit_grammar_status("start", slice_txt, result="queued")

                def _worker_wrapper() -> None:
                    try:
                        _run_llm_and_cache(
                            self.ctx,
                            cache_key,
                            fp,
                            aText,
                            n_start,
                            n_end,
                            seq,
                            map_key,
                        )
                    finally:
                        job.done.set()
                        with _INFLIGHT_LOCK:
                            if _INFLIGHT_JOBS.get(inflight_key) is job:
                                del _INFLIGHT_JOBS[inflight_key]

                run_in_background(
                    _worker_wrapper,
                    name="writeragent-grammar-proofread",
                    error_callback=_grammar_worker_error_callback,
                )
            else:
                log.info("[grammar] cache MISS joining in-flight worker fp=%s…", fp[:12])
                _emit_grammar_status("join", slice_txt, result="waiting")
            try:
                wait_timeout_ms = get_config_int(self.ctx, "doc.grammar_proofreader_wait_timeout_ms")
            except Exception as e:
                log.warning("[grammar] doProofreading: get_config_int wait_timeout_ms: %s", e, exc_info=True)
                wait_timeout_ms = 15000
            if _wait_for_inflight_job(self.ctx, job, wait_timeout_ms):
                cached_after_wait = engine.cache_get(cache_key, fp)
                if cached_after_wait is not None:
                    try:
                        a_res.aErrors = _cached_errors_to_uno_tuple(cached_after_wait)
                        log.info(
                            "[grammar] wait complete returning %s error(s) key=%s…",
                            len(tuple(a_res.aErrors)),
                            cache_key[:80],
                        )
                    except Exception as e:
                        log.exception("[grammar] doProofreading: post-wait cache path failed: %s", e)
                        try:
                            a_res.aErrors = ()
                        except Exception:
                            pass
                else:
                    log.info("[grammar] wait complete with no cache entry fp=%s…", fp[:12])
            else:
                log.info(
                    "[grammar] wait timed out after %sms; returning empty while worker continues fp=%s…",
                    wait_timeout_ms,
                    fp[:12],
                )
                _emit_grammar_status("timeout", slice_txt, result=f">{wait_timeout_ms}ms")
            return a_res
        except Exception as e:
            log.exception(
                "[grammar] doProofreading failed (returning empty errors if possible): %s",
                e,
            )
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
            return _build_empty_result(
                self,
                aDocumentIdentifier,
                aText,
                aLocale,
                nStartOfSentencePosition,
                nSuggestedBehindEndOfSentencePosition,
            )

    def ignoreRule(self, aRuleIdentifier: str, aLocale: Any) -> None:
        try:
            from plugin.modules.writer import grammar_proofread_engine as engine

            del aLocale  # locale-specific ignore not distinguished in cache yet
            engine.ignore_rule_add(str(aRuleIdentifier))
        except Exception as e:
            log.warning("[grammar] ignoreRule: %s", e, exc_info=True)

    def resetIgnoreRules(self) -> None:
        try:
            from plugin.modules.writer import grammar_proofread_engine as engine

            engine.ignore_rules_clear()
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


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    WriterAgentAiGrammarProofreader,
    IMPLEMENTATION_NAME,
    (SERVICE_NAME,),
)
