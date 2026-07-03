# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for the AI grammar proofreader (requires running LibreOffice)."""

from __future__ import annotations

from typing import Any
from plugin.writer.locale import ai_grammar_proofreader as proofreader
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.framework.config import get_config, set_config
from plugin.testing_runner import native_test, setup, teardown

_test_ctx: Any = None
_saved_enabled: Any = None

@setup
def setup_grammar_proof_tests(ctx: Any) -> None:
    global _test_ctx, _saved_enabled
    _test_ctx = ctx
    try:
        # This setting is a provider string now ("harper", "llm", "off", ...).
        # Saving it through get_config_bool() collapsed providers like "harper"
        # to False and teardown persisted the user's grammar checker as off.
        _saved_enabled = get_config("doc.grammar_proofreader_enabled")
    except Exception:
        _saved_enabled = "off"
    set_config("doc.grammar_proofreader_enabled", "llm")

@teardown
def teardown_grammar_proof_tests(ctx: Any) -> None:
    global _test_ctx, _saved_enabled
    if _saved_enabled is not None:
        set_config("doc.grammar_proofreader_enabled", _saved_enabled)
    gc.cache_clear()
    gc.clear_sentence_cache()
    gc.ignore_rules_clear()
    _test_ctx = None

@native_test
def test_do_proofreading_returns_cached_errors() -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Hello they is fine."
    from dataclasses import asdict

    norms = gt.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
        ctx=_test_ctx,
        loc_key="en-US",
    )
    gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-42")

    res = pr.doProofreading("doc-42", text, loc, 0, len(text), ())
    assert res is not None
    errs = tuple(res.aErrors)
    assert len(errs) == 1
    e0 = errs[0]
    assert text[e0.nErrorStart : e0.nErrorStart + e0.nErrorLength] == "they is"

@native_test
def test_ignore_rule_filters_cached_error() -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Hello they is fine."
    from dataclasses import asdict

    norms = gt.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
        ctx=_test_ctx,
        loc_key="en-US",
    )
    assert len(norms) == 1
    rid = norms[0].rule_identifier
    gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-99")
    res1 = pr.doProofreading("doc-99", text, loc, 0, len(text), ())
    assert len(tuple(res1.aErrors)) == 1
    pr.ignoreRule(rid, loc)
    res2 = pr.doProofreading("doc-99", text, loc, 0, len(text), ())
    assert len(tuple(res2.aErrors)) == 0
    pr.resetIgnoreRules()

@native_test
def test_incomplete_short_sentence_skips_before_cache_lookup() -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "Too short clause"
    from dataclasses import asdict

    norms = gt.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "short", "correct": "brief", "type": "style", "reason": "test"}],
        ctx=_test_ctx,
        loc_key="en-US",
    )
    gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-123")

    res = pr.doProofreading("doc-123", text, loc, 0, len(text), ())
    assert len(tuple(res.aErrors)) == 0

@native_test
def test_incomplete_long_sentence_uses_cache_when_allowed() -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    text = "This is a long unfinished sentence with bad grammar they is here"
    from dataclasses import asdict

    norms = gt.normalize_errors_for_text(
        text,
        0,
        len(text),
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
        ctx=_test_ctx,
        loc_key="en-US",
    )
    gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-124")

    res = pr.doProofreading("doc-124", text, loc, 0, len(text), ())
    assert len(tuple(res.aErrors)) == 1

@native_test
def test_paragraph_two_cached_sentences_return_both_errors() -> None:
    import uno
    from dataclasses import asdict
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    # Use words with 7+ alpha characters that exceed the 1-6 abbreviation threshold
    text = "Testing complete. Verification finished."
    sents = gt.split_into_sentences(_test_ctx, "en-US", text)
    assert len(sents) >= 2
    for off, st in sents[:2]:
        wrong = "complete" if "Testing" in st else "finished"
        norms = gt.normalize_errors_for_text(
            st,
            0,
            len(st),
            [{"wrong": wrong, "correct": "x", "type": "style", "reason": "t"}],
            ctx=_test_ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", st, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-pair")
    res = pr.doProofreading("doc-pair", text, loc, 0, len(text), ())
    assert len(tuple(res.aErrors)) == 2

@native_test
def test_incremental_nonzero_start_returns_only_overlapping_sentence() -> None:
    import uno
    from dataclasses import asdict
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    assert _test_ctx is not None
    pr = WriterAgentAiGrammarProofreader(_test_ctx)
    loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
    # Use words with 7+ alpha characters that exceed the 1-6 abbreviation threshold
    text = "Testing. Verification. Complete. Finished."
    sents = gt.split_into_sentences(_test_ctx, "en-US", text)
    assert len(sents) >= 3
    t_off, t_txt = sents[2]
    norms = gt.normalize_errors_for_text(
        t_txt,
        0,
        len(t_txt),
        [{"wrong": "Complete", "correct": "complete", "type": "spelling", "reason": "t"}],
        ctx=_test_ctx,
        loc_key="en-US",
    )
    gc.cache_put_sentence("en-US", t_txt, [asdict(n) for n in norms], ctx=_test_ctx, doc_id="doc-inc")
    res = pr.doProofreading("doc-inc", text, loc, t_off, t_off + len(t_txt), ())
    errs = tuple(res.aErrors)
    assert len(errs) == 1
    assert text[errs[0].nErrorStart : errs[0].nErrorStart + errs[0].nErrorLength] == "Complete"
