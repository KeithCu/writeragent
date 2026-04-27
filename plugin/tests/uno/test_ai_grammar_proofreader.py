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
    eng.clear_sentence_cache()
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
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
    )
    eng.cache_put_sentence("en-US", text, [asdict(n) for n in norms])

    res = pr.doProofreading("doc-42", text, loc, n_start, n_end, ())
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
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
    )
    assert len(norms) == 1
    rid = norms[0].rule_identifier
    eng.cache_put_sentence("en-US", text, [asdict(n) for n in norms])
    res1 = pr.doProofreading("doc-99", text, loc, n_start, n_end, ())
    assert len(tuple(res1.aErrors)) == 1
    pr.ignoreRule(rid, loc)
    res2 = pr.doProofreading("doc-99", text, loc, n_start, n_end, ())
    assert len(tuple(res2.aErrors)) == 0
    pr.resetIgnoreRules()


@native_test
def test_incomplete_short_sentence_skips_before_cache_lookup() -> None:
    import uno

    from plugin.modules.writer.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Too short clause"
    n_start = 0
    n_end = min(len(text), 500)
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "short", "correct": "brief", "type": "style", "reason": "test"}],
    )
    eng.cache_put_sentence("en-US", text[n_start:n_end], [asdict(n) for n in norms])

    res = pr.doProofreading("doc-123", text, loc, n_start, n_end, ())
    assert len(tuple(res.aErrors)) == 0


@native_test
def test_incomplete_long_sentence_uses_cache_when_allowed() -> None:
    import uno

    from plugin.modules.writer.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "This is a long unfinished sentence with bad grammar they is here"
    n_start = 0
    n_end = min(len(text), 500)
    slice_txt = text[n_start:n_end]
    from dataclasses import asdict

    norms = eng.normalize_errors_for_text(
        slice_txt,
        0,
        len(slice_txt),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
    )
    eng.cache_put_sentence("en-US", slice_txt, [asdict(n) for n in norms])

    res = pr.doProofreading("doc-124", text, loc, n_start, n_end, ())
    errs = tuple(res.aErrors)
    assert len(errs) == 1
