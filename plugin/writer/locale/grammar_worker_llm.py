# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync LLM orchestration for the grammar worker (chat completion, parsing, language detect)."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import grammar_proofread_cache, grammar_proofread_json, grammar_proofread_locale, grammar_persistence
from .grammar_obs import emit_grammar_status, grammar_obs, slice_preview_debug

from plugin.framework import queue_executor

log = logging.getLogger("writeragent.grammar")

# Mercury and other reasoning models may return null content when effort is unconstrained.
_GRAMMAR_CHAT_EXTRA = {"reasoning": {"effort": "minimal"}}


def get_cached_language(text: str) -> str | None:
    with grammar_persistence.grammar_registry.lock:
        if text in grammar_persistence.grammar_registry.lang_detect_cache:
            grammar_persistence.grammar_registry.lang_detect_cache.move_to_end(text)
            return grammar_persistence.grammar_registry.lang_detect_cache[text]
    return None


def put_cached_language(text: str, lang: str) -> None:
    with grammar_persistence.grammar_registry.lock:
        grammar_persistence.grammar_registry.lang_detect_cache[text] = lang
        if len(grammar_persistence.grammar_registry.lang_detect_cache) > 1000:
            grammar_persistence.grammar_registry.lang_detect_cache.popitem(last=False)


def persisted_grammar_skip_lang_detect(ctx: Any, doc_id: str, text: str) -> bool:
    """True if persistence already stores grammar for this sentence (fingerprint).

    Heuristic to skip redundant language-detect LLM on reopen: any stored row (including
    empty errors for 'good' sentences) implies prior proofreading — good enough to treat
    as language-resolved for this session. Wrong-locale clean rows could skip redetect.
    """
    try:
        if not doc_id:
            return False
        fp = grammar_proofread_cache.sentence_identity_fp(text)
        p = grammar_persistence.get_persistence(ctx, doc_id)
        return p is not None and p.get(fp) is not None
    except Exception as e:
        log.debug("[grammar] persisted grammar heuristic lookup failed: %s", e, exc_info=True)
        return False


def get_active_ignored_reasons(ctx: Any, doc_id: str) -> set[str]:
    """Document + global ignored grammar rules, normalized for prompt filtering."""
    from .grammar_persistence import get_persistence
    from .grammar_proofread_cache import ignored_rules_snapshot
    from .grammar_proofread_locale import normalize_reason

    p = get_persistence(ctx, doc_id)
    ignored_reasons = set(p._ignored_rules) if p else set()
    for r in ignored_rules_snapshot():
        if r.startswith("wa_g_rule||"):
            ignored_reasons.add(normalize_reason(r[11:]))
        else:
            ignored_reasons.add(normalize_reason(r))
    return ignored_reasons


def build_grammar_system_prompt(
    bcp47: str,
    ignored_reasons: set[str],
    *,
    batch: bool,
    any_partial: bool,
) -> str:
    lang_name = grammar_proofread_locale.grammar_english_name_for_bcp47(bcp47)
    if batch:
        sys_prompt = grammar_proofread_locale.GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=bcp47)
        if any_partial:
            sys_prompt += " The input may contain partial sentences; prefer conservative grammar suggestions and avoid broad rewrites."
    else:
        sys_prompt = grammar_proofread_locale.GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=lang_name, bcp47=bcp47)
        if any_partial:
            sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."
    if ignored_reasons:
        sys_prompt += "\n\nIMPORTANT: The user has explicitly chosen to IGNORE the following rules/style issues in this document. DO NOT report any errors or suggestions that match or are highly similar to these:\n"
        for reason in sorted(ignored_reasons):
            sys_prompt += f"- {reason}\n"
    return sys_prompt


def language_detect_llm_sync(ec: Any, messages: list[dict[str, str]], max_tokens: int) -> str:
    """Sync language-detect LLM call with one retry when the model returns empty content."""
    model = ec.model or None
    content = ec.client.chat_completion_sync(
        messages,
        max_tokens=max_tokens,
        model=model,
        response_format={"type": "json_object"},
        prepend_dev_build_system_prefix=False,
    )
    if (content or "").strip():
        return content
    grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=max_tokens)
    log.warning("[grammar] language detect returned empty content (max_tokens=%s model=%s); retrying with higher cap", max_tokens, model)
    retry_cap = max(max_tokens * 2, grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE)
    content = ec.client.chat_completion_sync(
        messages,
        max_tokens=retry_cap,
        model=model,
        response_format={"type": "json_object"},
        prepend_dev_build_system_prefix=False,
    )
    if not (content or "").strip():
        grammar_obs("lang_detect_empty_response", model=model or "", max_tokens=retry_cap, retry=True)
        log.warning("[grammar] language detect still empty after retry (max_tokens=%s)", retry_cap)
    return content or ""


def grammar_llm_sync(ec: Any, messages: list[dict[str, str]], max_tokens: int) -> str:
    """Sync grammar LLM call with minimal reasoning and one retry when content is empty."""
    model = ec.model or None
    kwargs = {
        "max_tokens": max_tokens,
        "model": model,
        "response_format": {"type": "json_object"},
        "prepend_dev_build_system_prefix": False,
        "chat_extra": _GRAMMAR_CHAT_EXTRA,
    }
    content = ec.client.chat_completion_sync(messages, **kwargs)
    if (content or "").strip():
        return content
    grammar_obs("grammar_llm_empty_response", model=model or "", max_tokens=max_tokens)
    log.warning("[grammar] grammar LLM returned empty content (max_tokens=%s model=%s); retrying with higher cap", max_tokens, model)
    retry_cap = max(max_tokens * 2, ec.max_tok)
    kwargs["max_tokens"] = retry_cap
    content = ec.client.chat_completion_sync(messages, **kwargs)
    if not (content or "").strip():
        grammar_obs("grammar_llm_empty_response", model=model or "", max_tokens=retry_cap, retry=True)
        log.warning("[grammar] grammar LLM still empty after retry (max_tokens=%s model=%s)", retry_cap, model)
    return content or ""


def call_grammar_llm(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    ec: Any,
) -> tuple[list[Any], int]:
    """Run grammar LLM for one sentence or a batch; return parsed results and elapsed ms."""
    batch = len(chunk) > 1
    doc_id = chunk[0][0].doc_id
    ignored_reasons = get_active_ignored_reasons(ec.ctx, doc_id)
    any_partial = any(item.partial_sentence or not grammar_proofread_locale.looks_complete_sentence(text) for item, text in chunk)
    sys_prompt = build_grammar_system_prompt(bcp47, ignored_reasons, batch=batch, any_partial=any_partial)

    if batch:
        user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(chunk))
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}]
        grammar_obs("worker_llm_batch_request", item_count=len(chunk), total_len=len(user_content))
        emit_grammar_status("request", f"Batch of {len(chunk)}", result="LLM batch request")
        max_tokens = ec.max_tok * grammar_proofread_locale.GRAMMAR_BATCH_MAX_SENTENCES
    else:
        item, text = chunk[0]
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}]
        grammar_obs("worker_llm_request_prepare", enqueue_seq=item.enqueue_seq, llm_text_len=len(text), llm_preview=slice_preview_debug(text, 96))
        emit_grammar_status("request", text, result="LLM request")
        max_tokens = ec.max_tok

    request_start = time.monotonic()
    with queue_executor.grammar_llm_request_gate(ec.ctx):
        content = grammar_llm_sync(ec, messages, max_tokens)
    elapsed_ms = int((time.monotonic() - request_start) * 1000)

    if batch:
        return grammar_proofread_json.parse_grammar_batch_json(content or ""), elapsed_ms
    sent_results = grammar_proofread_json.parse_grammar_json(content or "")
    return ([sent_results] if content else []), elapsed_ms


def _obs_lang_detect_item(idx: int, source: str, raw: str | None, canon: str | None, text: str) -> None:
    grammar_obs(
        "lang_detect_item",
        idx=idx,
        source=source,
        raw=raw,
        canon=canon,
        text_preview=slice_preview_debug(text, 48),
    )


def _detect_languages_via_langdetect(
    chunk: list[tuple[Any, str]],
    detected_langs: list[str | None],
    item_sources: list[str],
) -> None:
    """Fill pending slots in *detected_langs* using bundled langdetect (in-process, no LLM)."""
    from plugin.contrib.langdetect import detect_langs
    from plugin.contrib.langdetect.lang_detect_exception import LangDetectException

    pending = [idx for idx, src in enumerate(item_sources) if src == "pending"]
    if not pending:
        return

    if len(pending) == 1:
        idx = pending[0]
        text = chunk[idx][1]
        emit_grammar_status("request", text, result="Detecting language")
    else:
        emit_grammar_status("request", f"Batch of {len(pending)}", result="Detecting language")

    for idx in pending:
        text = chunk[idx][1]
        try:
            langs = detect_langs(text)
            raw = langs[0].lang if langs else None
        except LangDetectException:
            raw = None
        except Exception as e:
            log.warning("[grammar] langdetect failed for preview=%s: %s", slice_preview_debug(text, 48), e, exc_info=True)
            raw = None

        if raw:
            canon = grammar_proofread_locale.normalize_detected_bcp47(raw) or raw
            put_cached_language(text, canon)
            detected_langs[idx] = canon
            item_sources[idx] = "langdetect"
            _obs_lang_detect_item(idx, "langdetect", raw, canon, text)
        else:
            item_sources[idx] = "none"
            _obs_lang_detect_item(idx, "none", None, None, text)


def detect_languages_for_chunk(
    chunk: list[tuple[Any, str]],
    detect_lang_instruction: str,
    ec: Any,
    *,
    trust_persisted_grammar_as_lang: bool = True,
) -> list[str | None]:
    """Resolve BCP47 per sentence (cache, optional persistence heuristic, LLM, or langdetect)."""
    mode = getattr(ec, "detect_lang_mode", "llm") or "llm"
    if mode not in ("llm", "langdetect"):
        mode = "llm"
    detected_langs: list[str | None] = []
    item_sources: list[str] = []
    all_cached = True
    for idx, (item, text) in enumerate(chunk):
        cached = get_cached_language(text)
        if cached:
            canon = grammar_proofread_locale.normalize_detected_bcp47(cached) or cached
            detected_langs.append(canon)
            item_sources.append("cache")
            _obs_lang_detect_item(idx, "cache", cached, canon, text)
        elif trust_persisted_grammar_as_lang and persisted_grammar_skip_lang_detect(ec.ctx, item.doc_id, text):
            grammar_obs("lang_detect_skip", reason="persisted_grammar_heuristic", doc_id=item.doc_id[:32] if item.doc_id else "")
            canon = grammar_proofread_locale.normalize_detected_bcp47(ec.grammar_bcp47) or ec.grammar_bcp47
            put_cached_language(text, canon)
            detected_langs.append(canon)
            item_sources.append("persisted")
            _obs_lang_detect_item(idx, "persisted", ec.grammar_bcp47, canon, text)
        else:
            detected_langs.append(None)
            item_sources.append("pending")
            all_cached = False

    if not all_cached:
        if mode == "langdetect":
            _detect_languages_via_langdetect(chunk, detected_langs, item_sources)
        elif len(chunk) > 1:
            user_content = "\n".join(f"{idx+1}. {text}" for idx, (_it, text) in enumerate(chunk))
            detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_BATCH_SYSTEM_PROMPT.format(detect_lang_instruction=detect_lang_instruction)
            detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": user_content}]

            detect_max_tok = grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_PER_BATCH_ITEM * len(chunk)

            emit_grammar_status("request", f"Batch of {len(chunk)}", result="Detecting language")
            with queue_executor.grammar_llm_request_gate(ec.ctx):
                detect_content = language_detect_llm_sync(ec, detect_messages, detect_max_tok)

            parsed_langs = grammar_proofread_json.parse_language_detect_batch_json(detect_content or "")
            grammar_obs(
                "lang_detect_batch_parse",
                chunk_len=len(chunk),
                parsed_len=len(parsed_langs),
                ok=len(parsed_langs) == len(chunk),
            )
            if len(parsed_langs) == len(chunk):
                for idx, d_lang in enumerate(parsed_langs):
                    if d_lang:
                        canon = grammar_proofread_locale.normalize_detected_bcp47(d_lang) or d_lang
                        put_cached_language(chunk[idx][1], canon)
                        detected_langs[idx] = canon
                        item_sources[idx] = "llm"
                        _obs_lang_detect_item(idx, "llm", d_lang, canon, chunk[idx][1])
                    elif item_sources[idx] == "pending":
                        item_sources[idx] = "none"
                        _obs_lang_detect_item(idx, "none", None, None, chunk[idx][1])
            elif detect_content:
                log.warning("[grammar] language detect batch parse mismatch: chunk=%s parsed=%s", len(chunk), len(parsed_langs))
                for idx in range(len(chunk)):
                    if item_sources[idx] == "pending":
                        item_sources[idx] = "none"
                        _obs_lang_detect_item(idx, "none", None, None, chunk[idx][1])
        else:
            text = chunk[0][1]
            detect_prompt = grammar_proofread_locale.LANGUAGE_DETECT_SYSTEM_PROMPT.format(detect_lang_instruction=detect_lang_instruction)
            detect_messages = [{"role": "system", "content": detect_prompt}, {"role": "user", "content": text}]

            emit_grammar_status("request", text, result="Detecting language")
            with queue_executor.grammar_llm_request_gate(ec.ctx):
                detect_content = language_detect_llm_sync(
                    ec,
                    detect_messages,
                    grammar_proofread_locale.GRAMMAR_LANGUAGE_DETECT_MAX_TOKENS_SINGLE,
                )

            parsed_lang = grammar_proofread_json.parse_language_detect_json(detect_content or "")
            grammar_obs("lang_detect_single_parse", parsed=bool(parsed_lang), text_preview=slice_preview_debug(text, 48))
            if parsed_lang:
                canon = grammar_proofread_locale.normalize_detected_bcp47(parsed_lang) or parsed_lang
                put_cached_language(text, canon)
                detected_langs[0] = canon
                item_sources[0] = "llm"
                _obs_lang_detect_item(0, "llm", parsed_lang, canon, text)
            elif detect_content:
                log.warning("[grammar] language detect JSON parse failed for sentence preview=%s", slice_preview_debug(text, 48))
                if item_sources[0] == "pending":
                    item_sources[0] = "none"
                    _obs_lang_detect_item(0, "none", None, None, text)
            elif item_sources[0] == "pending":
                item_sources[0] = "none"
                _obs_lang_detect_item(0, "none", None, None, text)

    return detected_langs
