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

    def test_get_persistence_with_model_reuses_instance(self) -> None:
        """First get_persistence(..., model=) binds; later calls return the same instance."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp._doc_persistence_instances.clear()
        try:
            pa = gp.get_persistence(ctx, "2", model=model)
            pb = gp.get_persistence(ctx, "2")
            self.assertIsNotNone(pa)
            self.assertIs(pa, pb)
            self.assertIs(pa._model, model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_proofreading_doc_id_resolves_via_registered_active_model(self) -> None:
        """LO passes linguistic ids like '2', not RuntimeUID — cache must load via model= binding."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_persistence import DocumentPersistence

        ctx = MagicMock()
        model = MagicMock()
        cached = {
            "version": GRAMMAR_CACHE_VERSION,
            "good": [],
            "bad": {
                "fp_cached": [{"s": 0, "l": 3, "g": ["fix"], "c": "c", "f": "f", "r": "wa_g_rule||test"}],
            },
        }
        gp._doc_persistence_instances.clear()
        try:
            with patch("plugin.doc.document_helpers.get_document_property", return_value=json.dumps(cached)):
                dp = DocumentPersistence(ctx, "2", model=model)
                hit = dp.get("fp_cached")
            self.assertIsNotNone(hit)
            self.assertEqual(len(hit), 1)
            self.assertIs(dp._model, model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_lazy_bind_loads_udprops_after_init_without_model(self) -> None:
        """Cache embedded in the ODT must load once get_persistence(..., model=) binds the doc."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = {
            "version": GRAMMAR_CACHE_VERSION,
            "good": [],
            "bad": {
                "fp_cached": [{"s": 0, "l": 3, "g": ["fix"], "c": "c", "f": "f", "r": "wa_g_rule||test"}],
            },
        }
        gp._doc_persistence_instances.clear()
        try:
            with patch("plugin.doc.document_helpers.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "2")
                gp.grammar_registry.doc_persistence_instances["2"] = dp
                self.assertIsNone(dp._model)
                gp.get_persistence(ctx, "2", model=model)
                hit = dp.get("fp_cached")

            self.assertIsNotNone(hit)
            self.assertEqual(len(hit), 1)
            self.assertEqual(hit[0]["n_error_start"], 0)
            self.assertIs(dp._model, model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_document_persistence_persist_prunes_to_session(self) -> None:
        from plugin.writer.locale.grammar_persistence import DocumentPersistence

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.document_helpers.get_document_property", return_value=None):
            dp = DocumentPersistence(ctx, "doc-x", model=model)
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
        with patch("plugin.doc.document_helpers.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-save", model=model)

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
        with patch("plugin.doc.document_helpers.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-unload", model=model)

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
        with patch("plugin.doc.document_helpers.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-disp", model=model)

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
