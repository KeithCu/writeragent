# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import unittest
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_proofread_cache import normalize_reason
from plugin.writer.locale.grammar_persistence import DocumentPersistence
from plugin.writer.locale.ai_grammar_proofreader import _cached_errors_to_uno_tuple, WriterAgentAiGrammarProofreader

class TestGrammarIgnoreRules(unittest.TestCase):
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
        with (
            patch("plugin.writer.locale.grammar_persistence._find_model_by_runtime_uid", return_value=model),
            patch("plugin.doc.document_helpers.get_document_property", return_value=None),
        ):
            dp = DocumentPersistence(ctx, "doc-x")
            
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
        
        # Filter them just like grammar_work_queue does
        from plugin.writer.locale.grammar_proofread_cache import normalize_reason
        filtered_errors = []
        for e in norm_errors:
            rule_ident = e.rule_identifier
            if rule_ident.startswith("wa_g_rule||"):
                reason = rule_ident[11:]
                if normalize_reason(reason) in ignored:
                    continue
            filtered_errors.append(e)
        
        # The passive voice error should be skipped/thrown away
        self.assertEqual(len(filtered_errors), 1)
        self.assertEqual(filtered_errors[0].rule_identifier, "wa_g_rule||Use 'an' instead of 'a'.")

    def test_proofreader_ignore_and_reset_apis(self) -> None:
        ctx = MagicMock()
        pr = WriterAgentAiGrammarProofreader(ctx)
        
        model = MagicMock()
        dp = MagicMock()
        dp._ignored_rules = set()
        
        with (
            patch("plugin.framework.uno_context.get_active_document", return_value=model),
            patch("plugin.writer.locale.grammar_persistence._model_runtime_uid", return_value="doc-active"),
            patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=dp)
        ):
            # Ignore a rule
            pr.ignoreRule("wa_g_rule||Avoid passive voice.", None)
            self.assertIn("avoid passive voice", dp._ignored_rules)
            self.assertTrue(dp._persist_to_udprops.called)
            
            # Reset rules
            pr.resetIgnoreRules()
            self.assertEqual(len(dp._ignored_rules), 0)

if __name__ == "__main__":
    unittest.main()
