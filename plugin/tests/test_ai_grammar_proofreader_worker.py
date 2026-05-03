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

from plugin.modules.writer import ai_grammar_proofreader as proofreader
from plugin.modules.writer.grammar_proofread_engine import GrammarWorkItem


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
        patch("plugin.framework.llm_concurrency.is_agent_active", return_value=True),
        patch("plugin.modules.writer.ai_grammar_proofreader.time.sleep"),
        patch("plugin.modules.http.client.LlmClient") as client_cls,
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


def test_finalize_proofreading_uses_full_batch_end_not_suggested_prefix() -> None:
    """Lightproof-style batch: result positions extend to batch end, not LO’s growing n_suggested."""
    from plugin.modules.writer.ai_grammar_proofreader import _finalize_proofreading_sentence_positions

    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0

    text = "This is a sentence."
    proofread_end = min(len(text), proofreader.GRAMMAR_PROOFREAD_MAX_CHARS)
    r = Res()
    _finalize_proofreading_sentence_positions(r, text, n_suggested_behind_end=2, proofread_batch_end=proofread_end)
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
        patch("plugin.framework.llm_concurrency.is_agent_active", return_value=False),
        patch("plugin.framework.llm_concurrency.llm_request_lane") as lane_ctx,
        patch("plugin.modules.http.client.LlmClient") as client_cls,
        patch("plugin.modules.writer.ai_grammar_proofreader.time.sleep"),
        patch("plugin.modules.writer.grammar_proofread_engine.parse_grammar_json", return_value=[]),
        patch("plugin.modules.writer.grammar_proofread_engine.normalize_errors_for_text", return_value=[]),
        patch("plugin.modules.writer.grammar_proofread_engine.cache_put_sentence"),
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
        inflight_key="doc-1|en-US",
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
        inflight_key="doc-1|en-US",
        enqueue_seq=9,
    )
    q._latest_seq[item.inflight_key] = 9
    assert q._is_stale(item) is False
