# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for the AI grammar proofreader (requires running LibreOffice)."""

from __future__ import annotations

from typing import Any
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.framework.config import get_config, set_config
from plugin.testing_runner import native_test


def setup_grammar_proof_tests(_ctx: Any = None) -> Any:
    # This setting is a provider string now ("harper", "llm", "off", ...).
    # Saving it through get_config_bool() collapsed providers like "harper"
    # to False and teardown persisted the user's grammar checker as off.
    try:
        saved_enabled = get_config("doc.grammar_proofreader_enabled")
    except Exception:
        saved_enabled = "off"
    set_config("doc.grammar_proofreader_enabled", "llm")
    return saved_enabled


def teardown_grammar_proof_tests(saved_enabled: Any) -> None:
    set_config("doc.grammar_proofreader_enabled", saved_enabled)
    gc.cache_clear()
    gc.ignore_rules_clear()


@native_test
def test_do_proofreading_returns_cached_errors(ctx: Any) -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        text = "Hello they is fine."
        from dataclasses import asdict

        norms = gt.normalize_errors_for_text(
            text,
            0,
            len(text),
            [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
            ctx=ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-42")

        res = pr.doProofreading("doc-42", text, loc, 0, len(text), ())
        assert res is not None
        errs = tuple(res.aErrors)
        assert len(errs) == 1
        e0 = errs[0]
        assert text[e0.nErrorStart : e0.nErrorStart + e0.nErrorLength] == "they is"
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_ignore_rule_filters_cached_error(ctx: Any) -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        text = "Hello they is fine."
        from dataclasses import asdict

        norms = gt.normalize_errors_for_text(
            text,
            0,
            len(text),
            [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
            ctx=ctx,
            loc_key="en-US",
        )
        assert len(norms) == 1
        rid = norms[0].rule_identifier
        gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-99")
        res1 = pr.doProofreading("doc-99", text, loc, 0, len(text), ())
        assert len(tuple(res1.aErrors)) == 1

        # Test runs headless without an active UI component. Mock the doc ID lookup.
        import unittest.mock
        with unittest.mock.patch.object(pr, "_resolve_doc_id_for_ignore", return_value="doc-99"):
            pr.ignoreRule(rid, loc)
        res2 = pr.doProofreading("doc-99", text, loc, 0, len(text), ())
        assert len(tuple(res2.aErrors)) == 0
        with unittest.mock.patch.object(pr, "_resolve_doc_id_for_ignore", return_value="doc-99"):
            pr.resetIgnoreRules()
        res3 = pr.doProofreading("doc-99", text, loc, 0, len(text), ())
        assert len(tuple(res3.aErrors)) == 1
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_incomplete_short_sentence_skips_before_cache_lookup(ctx: Any) -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        text = "Too short clause"
        from dataclasses import asdict

        norms = gt.normalize_errors_for_text(
            text,
            0,
            len(text),
            [{"wrong": "short", "correct": "brief", "type": "style", "reason": "test"}],
            ctx=ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-123")

        res = pr.doProofreading("doc-123", text, loc, 0, len(text), ())
        assert len(tuple(res.aErrors)) == 0
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_incomplete_long_sentence_uses_cache_when_allowed(ctx: Any) -> None:
    import uno
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        text = "This is a long unfinished sentence with bad grammar they is here"
        from dataclasses import asdict

        norms = gt.normalize_errors_for_text(
            text,
            0,
            len(text),
            [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
            ctx=ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-124")

        res = pr.doProofreading("doc-124", text, loc, 0, len(text), ())
        assert len(tuple(res.aErrors)) == 1
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_paragraph_two_cached_sentences_return_both_errors(ctx: Any) -> None:
    import uno
    from dataclasses import asdict
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        # Use words with 7+ alpha characters that exceed the 1-6 abbreviation threshold
        text = "Testing complete. Verification finished."
        sents = gt.split_into_sentences(ctx, "en-US", text)
        assert len(sents) >= 2
        for off, st in sents[:2]:
            wrong = "complete" if "Testing" in st else "finished"
            norms = gt.normalize_errors_for_text(
                st,
                0,
                len(st),
                [{"wrong": wrong, "correct": "x", "type": "style", "reason": "t"}],
                ctx=ctx,
                loc_key="en-US",
            )
            gc.cache_put_sentence("en-US", st, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-pair")
        res = pr.doProofreading("doc-pair", text, loc, 0, len(text), ())
        assert len(tuple(res.aErrors)) == 2
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_incremental_nonzero_start_returns_only_overlapping_sentence(ctx: Any) -> None:
    import uno
    from dataclasses import asdict
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        pr = WriterAgentAiGrammarProofreader(ctx)
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", "en", "US", "")
        # Use words with 7+ alpha characters that exceed the 1-6 abbreviation threshold
        text = "Testing. Verification. Complete. Finished."
        sents = gt.split_into_sentences(ctx, "en-US", text)
        assert len(sents) >= 3
        t_off, t_txt = sents[2]
        norms = gt.normalize_errors_for_text(
            t_txt,
            0,
            len(t_txt),
            [{"wrong": "Complete", "correct": "complete", "type": "spelling", "reason": "t"}],
            ctx=ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", t_txt, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-inc")
        res = pr.doProofreading("doc-inc", text, loc, t_off, t_off + len(t_txt), ())
        errs = tuple(res.aErrors)
        assert len(errs) == 1
        assert text[errs[0].nErrorStart : errs[0].nErrorStart + errs[0].nErrorLength] == "Complete"
    finally:
        teardown_grammar_proof_tests(saved_enabled)


@native_test
def test_native_break_iterator_and_proofreader_emoji_utf16(ctx: Any) -> None:
    from dataclasses import asdict
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    saved_enabled = setup_grammar_proof_tests(ctx)
    try:
        bi, loc = gt.get_break_iterator_and_locale(ctx, "en-US")
        # Real BreakIterator.endOfSentence on "a👋b. c." returns UTF-16 units.
        # Tip: LO may include the trailing space (6) or not (5); slice in UTF-16 to check.
        sample = "a👋b. c."
        raw_end = bi.endOfSentence(sample, 0, loc)
        assert raw_end in (5, 6)
        assert sample.encode("utf-16le")[: raw_end * 2].decode("utf-16le").rstrip() == "a👋b."

        # split_into_sentences on "👋👋 Hi. Bye." returns "👋👋 Hi. " and "Bye."
        sents = gt.split_into_sentences(ctx, "en-US", "👋👋 Hi. Bye.")
        assert len(sents) == 2
        assert sents[0][1] == "👋👋 Hi. "
        assert sents[1][1] == "Bye."

        # doProofreading on "👋 they is fine." with cached error
        pr = WriterAgentAiGrammarProofreader(ctx)
        text = "👋 they is fine."
        norms = gt.normalize_errors_for_text(
            text,
            0,
            len(text),
            [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}],
            ctx=ctx,
            loc_key="en-US",
        )
        gc.cache_put_sentence("en-US", text, [asdict(n) for n in norms], ctx=ctx, doc_id="doc-uno-emoji")
        res = pr.doProofreading("doc-uno-emoji", text, loc, 0, len(text), ())
        assert res is not None
        errs = tuple(res.aErrors)
        assert len(errs) == 1
        e0 = errs[0]
        # In UTF-16, 👋 is 2 units, space is 1 unit -> start is 3, length is 7
        u16_bytes = text.encode("utf-16le")
        u16_slice = u16_bytes[e0.nErrorStart * 2 : (e0.nErrorStart + e0.nErrorLength) * 2].decode("utf-16le")
        assert u16_slice == "they is"
    finally:
        teardown_grammar_proof_tests(saved_enabled)
