# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_persistence import get_persistence, GRAMMAR_CACHE_VERSION

class TestGrammarPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = MagicMock()
        
    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_get_persistence_document_mode_per_doc_id(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        with patch.object(gp, "_find_model_by_runtime_uid", return_value=None):
            gp._doc_persistence_instances.clear()
            try:
                pa = gp.get_persistence(ctx, "runtime-a")
                pb = gp.get_persistence(ctx, "runtime-b")
                pa2 = gp.get_persistence(ctx, "runtime-a")
                self.assertIsNotNone(pa)
                self.assertIs(pa, pa2)
                self.assertIsNot(pa, pb)
            finally:
                gp.clear_all_document_persistence(ctx)

    def test_document_persistence_persist_prunes_to_session(self) -> None:
        from plugin.writer.locale.grammar_persistence import DocumentPersistence

        ctx = MagicMock()
        model = MagicMock()
        with (
            patch("plugin.writer.locale.grammar_persistence._find_model_by_runtime_uid", return_value=model),
            patch("plugin.doc.document_helpers.get_document_property", return_value=None),
        ):
            dp = DocumentPersistence(ctx, "doc-x")
        self.assertIsNone(dp.get("fp_missing"))
        dp.put("fp1", "en-US", [{"n_error_start": 0, "n_error_length": 1}])
        dp.put("fp2", "en-US", [])
        dp.get("fp1")
        with patch("plugin.doc.document_helpers.set_document_property") as mock_set:
            dp._persist_to_udprops()
        self.assertTrue(mock_set.called)
        args = mock_set.call_args[0]
        self.assertIs(args[0], model)
        written = json.loads(str(args[2]))
        self.assertEqual(written.get("version"), 2)
        self.assertIn("fp1", written.get("bad", {}))
        self.assertIn("fp2", written.get("good", []))
        self.assertEqual(written["bad"]["fp1"][0]["s"], 0)

    def test_document_event_on_save_triggers_persist(self) -> None:
        """documentEventOccured with OnSave should drive set_document_property."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with (
            patch("plugin.writer.locale.grammar_persistence._find_model_by_runtime_uid", return_value=model),
            patch("plugin.doc.document_helpers.get_document_property", return_value=None),
        ):
            dp = gp.DocumentPersistence(ctx, "doc-save")

        dp.put("fp_save", "en-US", [])
        listener = gp._GrammarDocumentEventListener(dp)
        save_event = MagicMock()
        save_event.EventName = "OnSave"

        with patch("plugin.doc.document_helpers.set_document_property") as mock_set:
            listener.documentEventOccured(save_event)

        self.assertTrue(mock_set.called, "OnSave must call set_document_property")
        args = mock_set.call_args[0]
        self.assertIs(args[0], model)
        written = json.loads(str(args[2]))
        self.assertIn("fp_save", written.get("good", []))

    def test_document_event_on_unload_triggers_teardown(self) -> None:
        """documentEventOccured with OnUnload should teardown and clear the cache."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with (
            patch("plugin.writer.locale.grammar_persistence._find_model_by_runtime_uid", return_value=model),
            patch("plugin.doc.document_helpers.get_document_property", return_value=None),
        ):
            dp = gp.DocumentPersistence(ctx, "doc-unload")

        dp.put("fp_x", "en-US", [])
        self.assertIsNotNone(dp.get("fp_x"))

        listener = gp._GrammarDocumentEventListener(dp)
        unload_event = MagicMock()
        unload_event.EventName = "OnUnload"
        listener.documentEventOccured(unload_event)

        self.assertTrue(dp._teardown_done)
        self.assertIsNone(dp.get("fp_x"))

    def test_document_event_listener_disposing_triggers_teardown(self) -> None:
        """The single combined listener also handles broadcaster ``disposing``."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with (
            patch("plugin.writer.locale.grammar_persistence._find_model_by_runtime_uid", return_value=model),
            patch("plugin.doc.document_helpers.get_document_property", return_value=None),
        ):
            dp = gp.DocumentPersistence(ctx, "doc-disp")

        listener = gp._GrammarDocumentEventListener(dp)
        listener.disposing(MagicMock())
        self.assertTrue(dp._teardown_done)

    def test_document_event_listener_has_correct_uno_method_names(self) -> None:
        """Guardrail: the listener must expose UNO IDL method names exactly."""
        from plugin.writer.locale import grammar_persistence as gp

        self.assertTrue(hasattr(gp._GrammarDocumentEventListener, "documentEventOccured"))
        self.assertTrue(hasattr(gp._GrammarDocumentEventListener, "disposing"))
        self.assertFalse(hasattr(gp._GrammarDocumentEventListener, "documentEvent"), "stale typo ``documentEvent`` must not be present")


if __name__ == "__main__":
    unittest.main()
