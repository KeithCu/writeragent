# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.locale.grammar_ignore_rules import (
    HARPER_RULE_PREFIX,
    LANGUAGETOOL_RULE_PREFIX,
    STABLE_RULE_PREFIXES,
    WA_G_RULE_PREFIX,
    collect_ignored_reasons,
    doc_ignored_rules,
    is_rule_ignored,
    make_rule_identifier,
    parse_prefixed_rule_identifier,
)
from plugin.writer.locale.grammar_proofread_locale import normalize_reason
from plugin.writer.locale.grammar_persistence import DocumentPersistence
from plugin.writer.locale.ai_grammar_proofreader import _cached_errors_to_uno_tuple, WriterAgentAiGrammarProofreader

_STABLE_PREFIX_CASES = (
    (HARPER_RULE_PREFIX, "SpellCheck", "SentenceCapitalization"),
    (LANGUAGETOOL_RULE_PREFIX, "ENGLISH_WORD_REPEAT_RULE", "UPPERCASE_SENTENCE_START"),
)


class TestGrammarIgnoreRules(unittest.TestCase):
    def test_is_rule_ignored_wa_g_rule_doc_match(self) -> None:
        rule = f"{WA_G_RULE_PREFIX}Avoid passive voice."
        self.assertTrue(is_rule_ignored(rule, {"avoid passive voice"}, set()))

    def test_is_rule_ignored_wa_g_rule_global_match(self) -> None:
        rule = f"{WA_G_RULE_PREFIX}Avoid passive voice."
        self.assertTrue(is_rule_ignored(rule, set(), {rule}))

    def test_is_rule_ignored_legacy_doc_match(self) -> None:
        self.assertTrue(is_rule_ignored("legacy-rule-id", {"legacy-rule-id"}, set()))

    def test_is_rule_ignored_not_ignored(self) -> None:
        rule = f"{WA_G_RULE_PREFIX}Use 'an' instead of 'a'."
        self.assertFalse(is_rule_ignored(rule, {"avoid passive voice"}, set()))

    def test_parse_prefixed_rule_identifier(self) -> None:
        for prefix in STABLE_RULE_PREFIXES:
            sample = "SpellCheck" if prefix == HARPER_RULE_PREFIX else "ENGLISH_WORD_REPEAT_RULE"
            self.assertEqual(parse_prefixed_rule_identifier(make_rule_identifier(prefix, sample), prefix), sample)
        self.assertIsNone(parse_prefixed_rule_identifier("wa_g_rule||reason", HARPER_RULE_PREFIX))
        self.assertIsNone(parse_prefixed_rule_identifier(f"{HARPER_RULE_PREFIX}", HARPER_RULE_PREFIX))

    def test_collect_ignored_reasons_merges_doc_and_global(self) -> None:
        ctx = MagicMock()
        dp = MagicMock()
        dp._ignored_rules = {"avoid passive voice"}
        global_rule = f"{WA_G_RULE_PREFIX}Use 'an' instead of 'a'."
        with (
            patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp),
            patch("plugin.writer.locale.grammar_ignore_rules.ignored_rules_snapshot", return_value={global_rule}),
        ):
            reasons = collect_ignored_reasons(ctx, "doc-x")
        self.assertIn("avoid passive voice", reasons)
        self.assertIn(normalize_reason(global_rule[len(WA_G_RULE_PREFIX) :]), reasons)

    def test_collect_ignored_reasons_stable_global_uses_bare_code(self) -> None:
        ctx = MagicMock()
        dp = MagicMock()
        dp._ignored_rules = set()
        global_rule = make_rule_identifier(HARPER_RULE_PREFIX, "SpellCheck")
        with (
            patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp),
            patch("plugin.writer.locale.grammar_ignore_rules.ignored_rules_snapshot", return_value={global_rule}),
        ):
            reasons = collect_ignored_reasons(ctx, "doc-x")
        self.assertIn("SpellCheck", reasons)
        self.assertNotIn("harper spellcheck", reasons)

    def test_doc_ignored_rules_empty_when_no_persistence(self) -> None:
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=None):
            self.assertEqual(doc_ignored_rules(MagicMock(), "missing"), set())

    def test_normalize_reason(self) -> None:
        self.assertEqual(normalize_reason("Avoid passive voice."), "avoid passive voice")
        self.assertEqual(normalize_reason("  Avoid    passive   voice. "), "avoid passive voice")
        self.assertEqual(
            normalize_reason("Use 'an' instead of 'a' before vowel sounds."),
            "use an instead of a before vowel sounds",
        )
        self.assertEqual(normalize_reason("Is this a question? Yes!"), "is this a question yes")
        self.assertEqual(normalize_reason(""), "")

    def test_document_persistence_stores_and_saves_ignored_rules(self) -> None:
        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.document_helpers.get_document_property", return_value=None):
            dp = DocumentPersistence(ctx, "doc-x", model=model)

        dp._ignored_rules.add("avoid passive voice")

        with patch("plugin.doc.document_helpers.set_document_property") as mock_set:
            dp._persist_to_udprops()

        self.assertTrue(mock_set.called)
        written = json.loads(str(mock_set.call_args[0][2]))
        self.assertIn("avoid passive voice", written["ignored_rules"])

    def test_cached_errors_to_uno_tuple_filters_ignored_rules(self) -> None:
        ctx = MagicMock()
        doc_id = "doc-x"
        dp = MagicMock()
        dp._ignored_rules = {"avoid passive voice"}

        cached = (
            {
                "n_error_start": 0,
                "n_error_length": 5,
                "suggestions": ("active",),
                "short_comment": "Avoid passive voice",
                "full_comment": "Avoid passive voice",
                "rule_identifier": "wa_g_rule||Avoid passive voice.",
            },
            {
                "n_error_start": 10,
                "n_error_length": 3,
                "suggestions": ("an",),
                "short_comment": "Use 'an'",
                "full_comment": "Use 'an'",
                "rule_identifier": "wa_g_rule||Use 'an' instead of 'a'.",
            },
        )

        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp):
            res = _cached_errors_to_uno_tuple(cached, ctx, doc_id)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].aRuleIdentifier, "wa_g_rule||Use 'an' instead of 'a'.")

    def test_normalize_errors_filters_ignored_rules(self) -> None:
        from plugin.writer.locale.grammar_proofread_text import normalize_errors_for_text
        from tests.writer.locale.test_grammar_proofread_text import FakeBI

        full_text = "This was done by him. This is a apple."
        items = [
            {"wrong": "was done", "correct": "active", "reason": "Avoid passive voice.", "type": "grammar"},
            {"wrong": "a apple", "correct": "an apple", "reason": "Use 'an' instead of 'a'.", "type": "grammar"},
        ]

        ignored = {"avoid passive voice"}
        with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
            norm_errors = normalize_errors_for_text(full_text, 0, len(full_text), items)

        filtered_errors = [e for e in norm_errors if not is_rule_ignored(e.rule_identifier, ignored, set())]
        self.assertEqual(len(filtered_errors), 1)
        self.assertEqual(filtered_errors[0].rule_identifier, "wa_g_rule||Use 'an' instead of 'a'.")

    def test_proofreader_ignore_and_reset_apis(self) -> None:
        ctx = MagicMock()
        pr = WriterAgentAiGrammarProofreader(ctx)
        pr._last_doc_id = "2"

        dp = MagicMock()
        dp._ignored_rules = set()

        with (
            patch("plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound"),
            patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp) as mock_get,
        ):
            pr.ignoreRule("wa_g_rule||Avoid passive voice.", None)
            mock_get.assert_called_with(ctx, "2")
            self.assertIn("avoid passive voice", dp._ignored_rules)
            self.assertTrue(dp._persist_to_udprops.called)

            pr.resetIgnoreRules()
            self.assertEqual(len(dp._ignored_rules), 0)


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_is_rule_ignored_stable_doc_code_only(prefix: str, ignored_code: str, other_code: str) -> None:
    rule = make_rule_identifier(prefix, ignored_code)
    assert is_rule_ignored(rule, {ignored_code}, set())


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_is_rule_ignored_stable_global(prefix: str, ignored_code: str, other_code: str) -> None:
    rule = make_rule_identifier(prefix, ignored_code)
    assert is_rule_ignored(rule, set(), {rule})


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_is_rule_ignored_stable_not_ignored(prefix: str, ignored_code: str, other_code: str) -> None:
    rule = make_rule_identifier(prefix, ignored_code)
    assert not is_rule_ignored(rule, {other_code}, set())


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_cached_errors_to_uno_tuple_filters_stable_ignored_rules(prefix: str, ignored_code: str, other_code: str) -> None:
    ctx = MagicMock()
    doc_id = "doc-x"
    dp = MagicMock()
    dp._ignored_rules = {ignored_code}

    cached = (
        {
            "n_error_start": 0,
            "n_error_length": 4,
            "suggestions": ("fix",),
            "short_comment": "ignored",
            "full_comment": "ignored",
            "rule_identifier": make_rule_identifier(prefix, ignored_code),
        },
        {
            "n_error_start": 10,
            "n_error_length": 3,
            "suggestions": ("Fix",),
            "short_comment": "other",
            "full_comment": "other",
            "rule_identifier": make_rule_identifier(prefix, other_code),
        },
    )

    with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp):
        res = _cached_errors_to_uno_tuple(cached, ctx, doc_id)

    assert len(res) == 1
    assert res[0].aRuleIdentifier == make_rule_identifier(prefix, other_code)


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_cached_errors_to_uno_tuple_filters_stable_after_reload(prefix: str, ignored_code: str, other_code: str) -> None:
    ctx = MagicMock()
    doc_id = "doc-x"
    dp = MagicMock()
    dp._ignored_rules = {ignored_code}

    cached = (
        {
            "n_error_start": 0,
            "n_error_length": 4,
            "suggestions": ("fix",),
            "short_comment": "ignored",
            "full_comment": "ignored",
            "rule_identifier": make_rule_identifier(prefix, ignored_code),
        },
    )

    with (
        patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp),
        patch("plugin.writer.locale.grammar_proofread_cache.ignored_rules_snapshot", return_value=set()),
    ):
        res = _cached_errors_to_uno_tuple(cached, ctx, doc_id)

    assert len(res) == 0


@pytest.mark.parametrize("prefix,ignored_code,other_code", _STABLE_PREFIX_CASES)
def test_proofreader_ignore_stable_rule(prefix: str, ignored_code: str, other_code: str) -> None:
    ctx = MagicMock()
    pr = WriterAgentAiGrammarProofreader(ctx)
    pr._last_doc_id = "2"

    dp = MagicMock()
    dp._ignored_rules = set()

    with (
        patch("plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound"),
        patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp),
    ):
        pr.ignoreRule(make_rule_identifier(prefix, ignored_code), None)

    assert ignored_code in dp._ignored_rules
    assert make_rule_identifier(prefix, ignored_code) not in dp._ignored_rules
    assert dp._persist_to_udprops.called


if __name__ == "__main__":
    unittest.main()
