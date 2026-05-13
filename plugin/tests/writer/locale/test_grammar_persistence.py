# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_persistence import JSONPersistence, SQLitePersistence, get_persistence, HAS_SQLITE
from plugin.writer.locale.grammar_proofread_locale import fingerprint_for_text

class TestGrammarPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = MagicMock()
        
    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    @unittest.skipUnless(HAS_SQLITE, "SQLite not available")
    def test_sqlite_persistence(self):
        db_path = os.path.join(self.tmp_dir, "test_grammar.db")
        p = SQLitePersistence(self.ctx, db_path)
        
        text = "This is a test."
        fp = fingerprint_for_text(text)
        errors = [{"wrong": "test", "correct": "TEST", "type": "grammar", "reason": "why not"}]
        
        p.put(fp, "en-US", errors)
        
        # New instance to verify persistence
        p2 = SQLitePersistence(self.ctx, db_path)
        hit = p2.get(fp)
        self.assertEqual(hit, errors)
        
        p2.clear()
        self.assertIsNone(p2.get(fp))

    @unittest.skipUnless(HAS_SQLITE, "SQLite not available")
    def test_sqlite_migration_drops_text_column(self):
        import sqlite3

        db_path = os.path.join(self.tmp_dir, "test_grammar_old_schema.db")
        text = "This sentence should not remain in SQLite."
        fp = fingerprint_for_text(text)
        errors = [{"wrong": "sentence", "correct": "Sentence", "type": "grammar", "reason": "capitalize"}]

        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE sentence_cache (
                    fingerprint TEXT PRIMARY KEY,
                    locale TEXT,
                    text TEXT,
                    errors_json TEXT,
                    last_used INTEGER
                )
            """)
            conn.execute("CREATE INDEX idx_last_used ON sentence_cache(last_used)")
            conn.execute(
                "INSERT INTO sentence_cache (fingerprint, locale, text, errors_json, last_used) VALUES (?, ?, ?, ?, ?)",
                (fp, "en-US", text, json.dumps(errors), 123),
            )
            conn.commit()

        p = SQLitePersistence(self.ctx, db_path)

        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(sentence_cache)").fetchall()]
        self.assertNotIn("text", columns)
        self.assertEqual(p.get(fp), errors)

    @unittest.skipUnless(HAS_SQLITE, "SQLite not available")
    def test_sqlite_does_not_persist_sentence_text(self):
        import sqlite3

        db_path = os.path.join(self.tmp_dir, "test_grammar_no_text.db")
        p = SQLitePersistence(self.ctx, db_path)

        text = "Plaintext should not be stored."
        fp = fingerprint_for_text(text)
        errors = [{"wrong": "Plaintext", "correct": "Plain text", "type": "grammar", "reason": "style"}]
        p.put(fp, "en-US", errors)

        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(sentence_cache)").fetchall()]
            row = conn.execute("SELECT * FROM sentence_cache WHERE fingerprint = ?", (fp,)).fetchone()

        self.assertEqual(columns, ["fingerprint", "locale", "errors_json", "last_used"])
        self.assertIsNotNone(row)
        self.assertNotIn(text, [str(value) for value in row])

    def test_json_persistence(self):
        dir_path = os.path.join(self.tmp_dir, "test_grammar_cache.d")
        p = JSONPersistence(self.ctx, dir_path)
        
        text = "This is a JSON test."
        fp = fingerprint_for_text(text)
        errors = [{"wrong": "test", "correct": "JSON_TEST", "type": "grammar", "reason": "fallback"}]
        
        p.put(fp, "en-US", errors)
        
        # Verify file exists
        self.assertTrue(os.path.exists(os.path.join(dir_path, f"{fp}.json")))
        
        # New instance
        p2 = JSONPersistence(self.ctx, dir_path)
        hit = p2.get(fp)
        self.assertEqual(hit, errors)
        
        p2.clear()
        self.assertIsNone(p2.get(fp))
        self.assertEqual(len(os.listdir(dir_path)), 0)

    def test_json_persistence_does_not_persist_sentence_text(self):
        dir_path = os.path.join(self.tmp_dir, "test_grammar_no_text_cache.d")
        p = JSONPersistence(self.ctx, dir_path)

        text = "This JSON plaintext should not be stored."
        fp = fingerprint_for_text(text)
        errors = [{"wrong": "JSON", "correct": "json", "type": "grammar", "reason": "style"}]
        p.put(fp, "en-US", errors)

        with open(os.path.join(dir_path, f"{fp}.json"), "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertNotIn("text", data)
        self.assertNotIn(text, json.dumps(data))

    @unittest.skipUnless(HAS_SQLITE, "SQLite not available")
    def test_sqlite_pruning(self):
        db_path = os.path.join(self.tmp_dir, "test_pruning.db")
        # Patch limits for testing
        with patch("plugin.writer.locale.grammar_persistence.CACHE_LIMIT", 5), \
             patch("plugin.writer.locale.grammar_persistence.PRUNE_TARGET", 2):
            p = SQLitePersistence(self.ctx, db_path)
            for i in range(10):
                txt = f"Sentence {i}"
                p.put(fingerprint_for_text(txt), "en-US", [])
            
            p.prune()
            
            # Verify count is PRUNE_TARGET (2)
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT count(*) FROM sentence_cache").fetchone()[0]
                self.assertEqual(count, 2)

    def test_json_pruning(self):
        dir_path = os.path.join(self.tmp_dir, "test_json_pruning.d")
        with patch("plugin.writer.locale.grammar_persistence.CACHE_LIMIT", 5), \
             patch("plugin.writer.locale.grammar_persistence.PRUNE_TARGET", 2):
            p = JSONPersistence(self.ctx, dir_path)
            for i in range(10):
                txt = f"Sentence {i}"
                p.put(fingerprint_for_text(txt), "en-US", [])
            
            p.prune()
            
            self.assertEqual(len(os.listdir(dir_path)), 2)

    def test_factory_and_singleton(self):
        # Reset singleton for testing (force SQLite/JSON path).
        with patch("plugin.writer.locale.grammar_persistence.USE_SQLITE_CACHE", True), patch(
            "plugin.writer.locale.grammar_persistence._persistence_instance", None
        ), patch("plugin.framework.config.user_config_dir", return_value=self.tmp_dir):
            p = get_persistence(self.ctx)
            self.assertIsNotNone(p)
            p2 = get_persistence(self.ctx)
            self.assertIs(p, p2)

            if HAS_SQLITE:
                self.assertIsInstance(p, SQLitePersistence)
            else:
                self.assertIsInstance(p, JSONPersistence)

    def test_get_persistence_document_mode_per_doc_id(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        with patch.object(gp, "USE_SQLITE_CACHE", False), patch.object(gp, "_find_model_by_runtime_uid", return_value=None):
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
        self.assertIn("fp1", written)
        self.assertIn("fp2", written)
        self.assertEqual(written["fp2"], [])

    def test_document_event_on_save_triggers_persist(self) -> None:
        """documentEventOccured with OnSave should drive set_document_property.

        Regression: the callback used to be named ``documentEvent``, which UNO never
        invokes — saves silently dropped on the floor. Now it must match the
        XDocumentEventListener IDL method name ``documentEventOccured``.
        """
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
        self.assertIn("fp_save", written)

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
        """Guardrail: the listener must expose UNO IDL method names exactly.

        ``XDocumentEventListener`` defines ``documentEventOccured`` (note the spelling)
        and inherits ``disposing`` from ``com.sun.star.lang.XEventListener``. A typo
        on either reverts the original silent-no-save bug.
        """
        from plugin.writer.locale import grammar_persistence as gp

        self.assertTrue(hasattr(gp._GrammarDocumentEventListener, "documentEventOccured"))
        self.assertTrue(hasattr(gp._GrammarDocumentEventListener, "disposing"))
        self.assertFalse(hasattr(gp._GrammarDocumentEventListener, "documentEvent"), "stale typo ``documentEvent`` must not be present")


if __name__ == "__main__":
    unittest.main()
