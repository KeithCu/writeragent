# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure phase decisions for the grammar worker (language validation + grammar completion)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .grammar_proofread_locale import grammar_bcp47_tags_match, normalize_detected_bcp47


@dataclass(frozen=True)
class LangRequeueAction:
    item: Any
    text: str
    new_bcp47: str
    original_bcp47: str


@dataclass(frozen=True)
class LanguageValidationDecision:
    """Outcome of comparing detected languages to the document target locale."""

    target_bcp47: str
    result_chunk: list[tuple[Any, str]]
    requeues: tuple[LangRequeueAction, ...] = ()


def decide_language_validation(
    chunk: list[tuple[Any, str]],
    target_bcp47: str,
    detected_langs: list[str | None],
) -> LanguageValidationDecision:
    """Map detected BCP47 tags to a filtered chunk and optional per-item requeues (pure)."""
    canon_target = normalize_detected_bcp47(target_bcp47) or target_bcp47

    if len(chunk) == 1:
        raw = detected_langs[0] if detected_langs else None
        d_lang = normalize_detected_bcp47(raw) if raw else None
        item, text = chunk[0]
        if d_lang and not grammar_bcp47_tags_match(d_lang, canon_target):
            return LanguageValidationDecision(target_bcp47=d_lang, result_chunk=[(item, text)])
        if d_lang:
            return LanguageValidationDecision(target_bcp47=d_lang, result_chunk=[(item, text)])
        return LanguageValidationDecision(target_bcp47=canon_target, result_chunk=list(chunk))

    matching: list[tuple[Any, str]] = []
    requeues: list[LangRequeueAction] = []
    for idx, raw in enumerate(detected_langs):
        item, text = chunk[idx]
        d_lang = normalize_detected_bcp47(raw) if raw else None
        if d_lang and not grammar_bcp47_tags_match(d_lang, canon_target):
            requeues.append(LangRequeueAction(item, text, d_lang, target_bcp47))
        elif d_lang:
            matching.append((item, text))
    return LanguageValidationDecision(target_bcp47=canon_target, result_chunk=matching, requeues=tuple(requeues))


@dataclass(frozen=True)
class GrammarCompletionDecision:
    requeue_all: bool
    apply_locale_after_success: bool


def decide_grammar_completion(
    chunk_len: int,
    results_len: int,
    bcp47: str,
    original_bcp47: str,
) -> GrammarCompletionDecision:
    """Whether to requeue the whole chunk or process results (and apply locale) after grammar LLM."""
    if results_len != chunk_len:
        return GrammarCompletionDecision(requeue_all=True, apply_locale_after_success=False)
    apply_locale = bool(original_bcp47 and not grammar_bcp47_tags_match(original_bcp47, bcp47))
    return GrammarCompletionDecision(requeue_all=False, apply_locale_after_success=apply_locale)
