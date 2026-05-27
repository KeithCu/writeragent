# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for pure grammar worker phase decisions."""

from __future__ import annotations

from plugin.writer.locale.grammar_worker_phases import (
    LangRequeueAction,
    decide_grammar_completion,
    decide_language_validation,
)
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem


def _item(key: str = "k1") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        text="Hello world.",
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="d",
        inflight_key=key,
        enqueue_seq=1,
    )


def test_decide_language_validation_ja_tag_matches_ja_jp() -> None:
    """LLM ``ja`` and document ``ja-JP`` must not trigger a locale change."""
    item = _item()
    decision = decide_language_validation([(item, item.text)], "ja-JP", ["ja"])
    assert decision.target_bcp47 == "ja-JP"
    assert decision.result_chunk == [(item, item.text)]
    assert decision.requeues == ()


def test_decide_language_validation_single_mismatch_updates_target() -> None:
    item = _item()
    decision = decide_language_validation([(item, item.text)], "en-US", ["fr-FR"])
    assert decision.target_bcp47 == "fr-FR"
    assert decision.result_chunk == [(item, item.text)]
    assert decision.requeues == ()


def test_decide_language_validation_multi_mismatch_requeues() -> None:
    a, b = _item("k1"), _item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", ["en-US", "fr-FR"])
    assert decision.target_bcp47 == "en-US"
    assert decision.result_chunk == [(a, a.text)]
    assert len(decision.requeues) == 1
    assert decision.requeues[0] == LangRequeueAction(b, b.text, "fr-FR", "en-US")


def test_decide_language_validation_all_match() -> None:
    a, b = _item("k1"), _item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", ["en-US", "en-US"])
    assert decision.result_chunk == [(a, a.text), (b, b.text)]
    assert decision.requeues == ()


def test_decide_grammar_completion_mismatch_requeues_all() -> None:
    decision = decide_grammar_completion(3, 2, "en-US", "en-US")
    assert decision.requeue_all is True
    assert decision.apply_locale_after_success is False


def test_decide_grammar_completion_success_with_locale_change() -> None:
    decision = decide_grammar_completion(1, 1, "ja-JP", "zh-CN")
    assert decision.requeue_all is False
    assert decision.apply_locale_after_success is True


def test_decide_grammar_completion_no_apply_when_tags_equivalent() -> None:
    decision = decide_grammar_completion(1, 1, "ja-JP", "ja")
    assert decision.apply_locale_after_success is False


def test_decide_grammar_completion_success_same_locale() -> None:
    decision = decide_grammar_completion(2, 2, "en-US", "en-US")
    assert decision.requeue_all is False
    assert decision.apply_locale_after_success is False
