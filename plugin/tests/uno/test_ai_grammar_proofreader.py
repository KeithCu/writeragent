# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for AI grammar proofreader (cache path, struct shape)."""

from __future__ import annotations

from typing import Any

from plugin.framework.config import get_config_bool, set_config
from plugin.modules.writer import grammar_proofread_engine as eng
from plugin.testing_runner import native_test, setup, teardown

_test_ctx: Any = None
_saved_enabled: Any = None


@setup
def setup_grammar_proof_tests(ctx: Any) -> None:
    global _test_ctx, _saved_enabled
    _test_ctx = ctx
    try:
        _saved_enabled = get_config_bool(ctx, "doc.grammar_proofreader_enabled")
    except Exception:
        _saved_enabled = False
    set_config(ctx, "doc.grammar_proofreader_enabled", True)


@teardown
def teardown_grammar_proof_tests(ctx: Any) -> None:
    global _test_ctx, _saved_enabled
    if _saved_enabled is not None:
        set_config(ctx, "doc.grammar_proofreader_enabled", _saved_enabled)
    eng.cache_clear()
    eng.ignore_rules_clear()
    _test_ctx = None


@native_test
def test_do_proofreading_returns_cached_errors() -> None:
    import uno

    from plugin.modules.writer.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Hello they is fine."
    n_start = 0
    n_end = len(text)
    fp = eng.fingerprint_for_text(text[n_start:n_end])
    key = eng.make_cache_key(42, "en_US_", fingerprint=fp)
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        text,
        n_start,
        n_end,
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
    )
    eng.cache_put(key, fp, [asdict(n) for n in norms])

    res = pr.doProofreading(42, text, loc, n_start, n_end, ())
    assert res is not None
    errs = tuple(res.aErrors)
    assert len(errs) == 1
    e0 = errs[0]
    assert e0.nErrorLength > 0
    assert text[e0.nErrorStart : e0.nErrorStart + e0.nErrorLength] == "they is"


@native_test
def test_ignore_rule_filters_cached_error() -> None:
    import uno

    from plugin.modules.writer.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Hello they is fine."
    n_start = 0
    n_end = len(text)
    fp = eng.fingerprint_for_text(text[n_start:n_end])
    key = eng.make_cache_key(99, "en_US_", fingerprint=fp)
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        text,
        n_start,
        n_end,
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
    )
    assert len(norms) == 1
    rid = norms[0].rule_identifier
    eng.cache_put(key, fp, [asdict(n) for n in norms])
    res1 = pr.doProofreading(99, text, loc, n_start, n_end, ())
    assert len(tuple(res1.aErrors)) == 1
    pr.ignoreRule(rid, loc)
    res2 = pr.doProofreading(99, text, loc, n_start, n_end, ())
    assert len(tuple(res2.aErrors)) == 0
    pr.resetIgnoreRules()
