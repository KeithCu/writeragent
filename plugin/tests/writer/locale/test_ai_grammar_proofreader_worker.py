from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


lang = _ensure_module("com.sun.star.lang")
ling = _ensure_module("com.sun.star.linguistic2")
setattr(lang, "Locale", type("Locale", (), {}))
setattr(lang, "XServiceDisplayName", type("XServiceDisplayName", (), {}))
setattr(lang, "XServiceInfo", type("XServiceInfo", (), {}))
setattr(lang, "XServiceName", type("XServiceName", (), {}))
setattr(ling, "XProofreader", type("XProofreader", (), {}))
setattr(ling, "XSupportedLocales", type("XSupportedLocales", (), {}))
unohelper_mod = _ensure_module("unohelper")
setattr(unohelper_mod, "Base", type("UnohelperBase", (object,), {}))
setattr(
    unohelper_mod,
    "ImplementationHelper",
    type(
        "ImplementationHelper",
        (),
        {"addImplementation": lambda self, *_args, **_kwargs: None},
    ),
)

import pytest
from unittest.mock import MagicMock, patch

class FakeBI:
    def getWordBoundary(self, text, pos, locale, wordType, bDirection):
        import re
        res = MagicMock()
        m = re.compile(r"\w+|\W+").match(text, pos)
        if m:
            res.startPos = pos + m.start()
            res.endPos = pos + m.end()
        else:
            res.startPos = pos
            res.endPos = len(text)
        return res
        
    def endOfSentence(self, text, pos, locale):
        import re
        m = re.search(r'[.!?]', text[pos:])
        if m:
            return pos + m.end()
        return len(text)

@pytest.fixture(autouse=True)
def mock_bi():
    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
        yield

from plugin.writer.locale import ai_grammar_proofreader as proofreader
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem


def test_worker_skips_when_agent_active_and_pause_enabled() -> None:
    def _get_config_bool(_ctx, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled":
            return True
        if key == "doc.grammar_proofreader_pause_during_agent":
            return True
        raise AssertionError(f"unexpected key: {key}")

    with (
        patch(
            "plugin.framework.config.get_config_int",
            return_value=0,
        ),
        patch(
            "plugin.framework.config.get_config_bool",
            side_effect=_get_config_bool,
        ),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=True),
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
    ):
        proofreader._run_llm_and_cache(
            ctx=None,
            full_text="test",
            n_start=0,
            n_end=4,
            enqueue_seq=3,
            inflight_key="doc|en",
            grammar_bcp47="en-US",
        )

    client_cls.assert_not_called()


def test_apply_proofreading_end_positions_skips_space_after_sentence() -> None:
    """Sentence-sized path: traversal advances past spaces after the checked span end."""
    from plugin.writer.locale.ai_grammar_proofreader import _apply_proofreading_end_positions

    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0

    text = "Hi. Bye."
    r = Res()
    _apply_proofreading_end_positions(r, text, 3)
    assert r.nStartOfNextSentencePosition == 4
    assert r.nBehindEndOfSentencePosition == 4


def test_apply_proofreading_end_positions_skips_tab_after_sentence() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import _apply_proofreading_end_positions

    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0

    text = "Hi.\tBye."
    r = Res()
    _apply_proofreading_end_positions(r, text, 3)
    assert r.nStartOfNextSentencePosition == 4
    assert r.nBehindEndOfSentencePosition == 4


def _legacy_lightproof_finalize_positions(a_res: object, a_text: str, n_suggested_behind_end: int, proofread_batch_end: int) -> None:
    """Historical capped-batch helper (no longer used by sentence-sized ``doProofreading``); kept for regression."""
    from plugin.writer.locale.ai_grammar_proofreader import _advance_past_leading_whitespace

    n_next = proofread_batch_end
    if n_next < len(a_text):
        n_next = _advance_past_leading_whitespace(a_text, n_next)
        ch = a_text[n_next : n_next + 1] if n_next < len(a_text) else ""
        if n_next == n_suggested_behind_end and ch != "":
            n_next = n_suggested_behind_end + 1
    a_res.nStartOfNextSentencePosition = n_next
    a_res.nBehindEndOfSentencePosition = n_next


def test_legacy_lightproof_finalize_uses_full_batch_end_not_suggested_prefix() -> None:
    """Pre-sentence-sized Lightproof batch: positions extend to batch end (regression only)."""
    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0

    text = "This is a sentence."
    proofread_end = min(len(text), proofreader.GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS)
    r = Res()
    _legacy_lightproof_finalize_positions(r, text, n_suggested_behind_end=2, proofread_batch_end=proofread_end)
    assert r.nStartOfNextSentencePosition == len(text)
    assert r.nBehindEndOfSentencePosition == len(text)


def test_sentence_terminators_cover_multilingual_cases() -> None:
    assert proofreader._looks_complete_sentence("Hello world.")
    assert proofreader._looks_complete_sentence("مرحبا بالعالم؟")
    assert proofreader._looks_complete_sentence("これは文です。")
    assert proofreader._looks_complete_sentence("यह एक वाक्य है।")
    assert not proofreader._looks_complete_sentence("incomplete clause")


def test_partial_threshold_counts_nonspace_chars() -> None:
    assert proofreader._count_nonspace_chars("a b c") == 3
    assert proofreader._count_nonspace_chars("too short") < proofreader.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
    assert (
        proofreader._count_nonspace_chars("this is long enough")
        >= proofreader.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
    )


def test_run_llm_skips_split_when_proofread_sentence_text_set() -> None:
    """Worker must not re-split when main thread pinned ``proofread_sentence_text`` (BreakIterator sync)."""

    def _get_config_bool(_ctx: object, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled":
            return True
        if key == "doc.grammar_proofreader_pause_during_agent":
            return False
        raise AssertionError(f"unexpected key: {key}")

    def _split_must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("split_into_sentences must not run when proofread_sentence_text is set")

    with (
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.config.get_config_str", return_value=""),
        patch("plugin.framework.config.get_text_model", return_value="test-model"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=False),
        patch("plugin.framework.queue_executor.llm_request_lane") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.writer.locale.grammar_work_queue.split_into_sentences", side_effect=_split_must_not_run),
        patch("plugin.writer.locale.grammar_proofread_locale.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        lane_ctx.return_value.__enter__ = MagicMock(return_value=None)
        lane_ctx.return_value.__exit__ = MagicMock(return_value=False)
        client_cls.return_value.chat_completion_sync.return_value = '{"errors":[]}'
        proofreader._run_llm_and_cache(
            ctx=None,
            full_text="Hello.",
            n_start=0,
            n_end=6,
            enqueue_seq=1,
            inflight_key="d|en-US|0",
            grammar_bcp47="en-US",
            proofread_sentence_text="Hello.",
        )


def test_partial_sentence_adds_prompt_note() -> None:
    def _get_config_bool(_ctx, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled":
            return True
        if key == "doc.grammar_proofreader_pause_during_agent":
            return False
        raise AssertionError(f"unexpected key: {key}")

    with (
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.config.get_config_str", return_value=""),
        patch("plugin.framework.config.get_text_model", return_value="test-model"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=False),
        patch("plugin.framework.queue_executor.llm_request_lane") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.writer.locale.grammar_proofread_locale.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        lane_ctx.return_value.__enter__ = MagicMock(return_value=None)
        lane_ctx.return_value.__exit__ = MagicMock(return_value=False)
        client = client_cls.return_value
        client.chat_completion_sync.return_value = '{"errors":[]}'
        proofreader._run_llm_and_cache(
            ctx=None,
            full_text="This is long enough but unfinished",
            n_start=0,
            n_end=len("This is long enough but unfinished"),
            enqueue_seq=0,
            inflight_key="doc|en-US",
            grammar_bcp47="en-US",
            partial_sentence=True,
        )

    args, kwargs = client.chat_completion_sync.call_args
    del kwargs
    messages = args[0]
    system_prompt = messages[0]["content"]
    assert "partial sentence" in system_prompt


def test_queue_stale_check_uses_latest_sequence() -> None:
    q = proofreader._GrammarWorkQueue()
    item = GrammarWorkItem(
        ctx=None,
        full_text="What is going on",
        n_start=0,
        n_end=len("What is going on"),
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=7,
    )
    q._latest_seq[item.inflight_key] = 9
    assert q._is_stale(item) is True


def test_queue_stale_check_allows_latest_item() -> None:
    q = proofreader._GrammarWorkQueue()
    item = GrammarWorkItem(
        ctx=None,
        full_text="What is going on",
        n_start=0,
        n_end=len("What is going on"),
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=9,
    )
    q._latest_seq[item.inflight_key] = 9
    assert q._is_stale(item) is False


def test_enqueue_replace_in_place() -> None:
    q = proofreader._GrammarWorkQueue()
    # Prevent the background worker from starting so we can inspect the queue
    q._worker_started = True
    item1 = GrammarWorkItem(
        ctx=None,
        full_text="First version",
        n_start=0,
        n_end=13,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=1,
    )
    item2 = GrammarWorkItem(
        ctx=None,
        full_text="Second version",
        n_start=0,
        n_end=14,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=2,
    )

    q.enqueue(item1)
    assert len(list(q._q.queue)) == 1
    assert q._q.queue[0].enqueue_seq == 1

    # Second item with same key should replace the first
    q.enqueue(item2)
    assert len(list(q._q.queue)) == 1
    assert q._q.queue[0].enqueue_seq == 2
    assert q._q.queue[0].full_text == "Second version"


def test_enqueue_skip_stale_duplicate() -> None:
    q = proofreader._GrammarWorkQueue()
    # Prevent the background worker from starting so we can inspect the queue
    q._worker_started = True
    item1 = GrammarWorkItem(
        ctx=None,
        full_text="Newer version",
        n_start=0,
        n_end=13,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=10,
    )
    item2 = GrammarWorkItem(
        ctx=None,
        full_text="Stale version",
        n_start=0,
        n_end=13,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="doc-1",
        inflight_key="doc-1|en-US|0",
        enqueue_seq=5,
    )

    q.enqueue(item1)
    assert q._q.queue[0].enqueue_seq == 10

    # Stale item with same key should be ignored
    q.enqueue(item2)
    assert len(list(q._q.queue)) == 1
    assert q._q.queue[0].enqueue_seq == 10


def test_candidate_sentence_spans_paragraph_includes_all() -> None:
    from unittest.mock import patch

    from plugin.writer.locale.ai_grammar_proofreader import candidate_sentence_spans_for_proofreading

    with patch(
        "plugin.writer.locale.grammar_proofread_text.split_into_sentences",
        return_value=[(0, "A."), (4, "B.")],
    ):
        spans = candidate_sentence_spans_for_proofreading(None, "en-US", "A. B.", 0, len("A. B."))
    assert len(spans) == 2
    assert spans[1][2] == "B."


def test_candidate_sentence_spans_incremental_second_sentence_only() -> None:
    from unittest.mock import patch

    from plugin.writer.locale.ai_grammar_proofreader import candidate_sentence_spans_for_proofreading

    with patch(
        "plugin.writer.locale.grammar_proofread_text.split_into_sentences",
        return_value=[(0, "A."), (4, "B.")],
    ):
        spans = candidate_sentence_spans_for_proofreading(None, "en-US", "A. B.", 4, 8)
    assert len(spans) == 1
    assert spans[0][2] == "B."


def test_filter_sentence_spans_drops_short_incomplete() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import filter_sentence_spans_for_thresholds

    assert filter_sentence_spans_for_thresholds([(0, 12, "Still typing")]) == []


def test_worker_one_llm_call_per_sentence_when_slice_splits() -> None:
    def _get_config_bool(_ctx, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled":
            return True
        if key == "doc.grammar_proofreader_pause_during_agent":
            return False
        raise AssertionError(f"unexpected key: {key}")

    with (
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.config.get_config_str", return_value=""),
        patch("plugin.framework.config.get_text_model", return_value="m"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=False),
        patch("plugin.framework.queue_executor.llm_request_lane") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.grammar_work_queue.split_into_sentences") as split_mock,
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
        patch("plugin.writer.locale.grammar_proofread_locale.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.ignored_rules_snapshot", return_value=frozenset()),
    ):
        lane_ctx.return_value.__enter__ = MagicMock(return_value=None)
        lane_ctx.return_value.__exit__ = MagicMock(return_value=False)
        split_mock.return_value = [(0, "A. "), (3, "B.")]
        client = client_cls.return_value
        client.chat_completion_sync.return_value = '{"errors":[]}'
        proofreader._run_llm_and_cache(
            ctx=None,
            full_text="A. B.",
            n_start=0,
            n_end=len("A. B."),
            enqueue_seq=1,
            inflight_key="d|en-US|0",
            grammar_bcp47="en-US",
        )
    assert client.chat_completion_sync.call_count == 2
    bodies = [call[0][0][1]["content"] for call in client.chat_completion_sync.call_args_list]
    assert bodies == ["A. ", "B."]
