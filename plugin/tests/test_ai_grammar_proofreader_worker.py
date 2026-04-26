from __future__ import annotations

import sys
import types
from unittest.mock import patch


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


def test_worker_skips_when_agent_active_and_pause_enabled() -> None:
    proofreader._DEBOUNCE_SEQ["doc|en"] = 3

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
            cache_key="cache",
            fingerprint="fp",
            full_text="test",
            n_start=0,
            n_end=4,
            debounce_seq=3,
            map_key="doc|en",
        )

    client_cls.assert_not_called()


def test_finalize_proofreading_uses_full_batch_end_not_suggested_prefix() -> None:
    """Lightproof-style batch: result positions extend to batch end, not LO’s growing n_suggested."""
    from plugin.modules.writer.ai_grammar_proofreader import _finalize_proofreading_sentence_positions

    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0

    text = "This is a sentence."
    proofread_end = min(len(text), 8000)
    r = Res()
    _finalize_proofreading_sentence_positions(r, text, n_suggested_behind_end=2, proofread_batch_end=proofread_end)
    assert r.nStartOfNextSentencePosition == len(text)
    assert r.nBehindEndOfSentencePosition == len(text)
