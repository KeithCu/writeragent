# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_ignore_rules import (
    WA_G_RULE_PREFIX,
    collect_ignored_reasons,
    doc_ignored_rules,
    is_rule_ignored,
)
from plugin.writer.locale.grammar_proofread_locale import normalize_reason
from plugin.writer.locale.grammar_persistence import DocumentPersistence
from plugin.writer.locale.ai_grammar_proofreader import _cached_errors_to_uno_tuple, WriterAgentAiGrammarProofreader

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

    def test_doc_ignored_rules_empty_when_no_persistence(self) -> None:
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=None):
            self.assertEqual(doc_ignored_rules(MagicMock(), "missing"), set())

    def test_normalize_reason(self) -> None:
        # Test basic lowercase and whitespace collapse
        self.assertEqual(normalize_reason("Avoid passive voice."), "avoid passive voice")
        self.assertEqual(normalize_reason("  Avoid    passive   voice. "), "avoid passive voice")
        
        # Test quote characters are stripped but their contents are preserved
        self.assertEqual(
            normalize_reason("Use 'an' instead of 'a' before vowel sounds."),
            "use an instead of a before vowel sounds"
        )
        self.assertEqual(
            normalize_reason('Use "an" instead of "a" before vowel sounds.'),
            "use an instead of a before vowel sounds"
        )
        self.assertEqual(
            normalize_reason("Use \u2018an\u2019 instead of \u201ca\u201d before vowel sounds."),
            "use an instead of a before vowel sounds"
        )
        
        # Test punctuation stripping
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
        args = mock_set.call_args[0]
        written = json.loads(str(args[2]))
        self.assertIn("ignored_rules", written)
        self.assertIn("avoid passive voice", written["ignored_rules"])

    def test_cached_errors_to_uno_tuple_filters_ignored_rules(self) -> None:
        ctx = MagicMock()
        doc_id = "doc-x"
        
        # Mock persistence
        dp = MagicMock()
        dp._ignored_rules = {"avoid passive voice"}
        
        cached = (
            {
                "n_error_start": 0,
                "n_error_length": 5,
                "suggestions": ("active",),
                "short_comment": "Avoid passive voice",
                "full_comment": "Avoid passive voice",
                "rule_identifier": "wa_g_rule||Avoid passive voice."
            },
            {
                "n_error_start": 10,
                "n_error_length": 3,
                "suggestions": ("an",),
                "short_comment": "Use 'an'",
                "full_comment": "Use 'an'",
                "rule_identifier": "wa_g_rule||Use 'an' instead of 'a'."
            }
        )
        
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp):
            res = _cached_errors_to_uno_tuple(cached, ctx, doc_id)
            
        # First error (avoid passive voice) should be ignored
        # Second error (use an) should NOT be ignored
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].aRuleIdentifier, "wa_g_rule||Use 'an' instead of 'a'.")

    def test_normalize_errors_filters_ignored_rules(self) -> None:
        from plugin.writer.locale.grammar_proofread_text import normalize_errors_for_text
        from tests.writer.locale.test_grammar_proofread_text import FakeBI
        
        full_text = "This was done by him. This is a apple."
        items = [
            {
                "wrong": "was done",
                "correct": "active",
                "reason": "Avoid passive voice.",
                "type": "grammar"
            },
            {
                "wrong": "a apple",
                "correct": "an apple",
                "reason": "Use 'an' instead of 'a'.",
                "type": "grammar"
            }
        ]
        
        ignored = {"avoid passive voice"}
        with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
            norm_errors = normalize_errors_for_text(full_text, 0, len(full_text), items)

        filtered_errors = [e for e in norm_errors if not is_rule_ignored(e.rule_identifier, ignored, set())]
        # The passive voice error should be skipped/thrown away
        self.assertEqual(len(filtered_errors), 1)
        self.assertEqual(filtered_errors[0].rule_identifier, "wa_g_rule||Use 'an' instead of 'a'.")

    def test_proofreader_ignore_and_reset_apis(self) -> None:
        ctx = MagicMock()
        pr = WriterAgentAiGrammarProofreader(ctx)
        pr._last_doc_id = "2"

        model = MagicMock()
        dp = MagicMock()
        dp._ignored_rules = set()

        with (
            patch("plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound"),
            patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp) as mock_get,
        ):
            # Ignore a rule
            pr.ignoreRule("wa_g_rule||Avoid passive voice.", None)
            mock_get.assert_called_with(ctx, "2")
            self.assertIn("avoid passive voice", dp._ignored_rules)
            self.assertTrue(dp._persist_to_udprops.called)
            
            # Reset rules
            pr.resetIgnoreRules()
            self.assertEqual(len(dp._ignored_rules), 0)

if __name__ == "__main__":
    unittest.main()
