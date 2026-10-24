# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import tempfile
import threading
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_persistence import GRAMMAR_CACHE_VERSION

_TEST_IDENT = "llm:test"


def _v3_payload(*, good: list | None = None, bad: dict | None = None) -> dict:
    """Locale-blind blob. Version stays 3 even after the writer moved to v4."""
    return {
        "version": 3,
        "model": _TEST_IDENT,
        "good": good or [],
        "bad": bad or {},
        "ignored_rules": [],
    }

class TestGrammarPersistence:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = MagicMock()
        
    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_get_persistence_document_mode_per_doc_id(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        gp.grammar_registry.doc_persistence_instances.clear()
        try:
            pa = gp.get_persistence(ctx, "runtime-a")
            pb = gp.get_persistence(ctx, "runtime-b")
            pa2 = gp.get_persistence(ctx, "runtime-a")
            assert (pa) is not None
            assert (pa) is (pa2)
            assert (pa) is not (pb)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_get_persistence_with_model_reuses_instance(self) -> None:
        """First get_persistence(..., model=) binds; later calls return the same instance."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.doc_persistence_instances.clear()
        try:
            pa = gp.get_persistence(ctx, "2", model=model)
            pb = gp.get_persistence(ctx, "2")
            assert (pa) is not None
            assert (pa) is (pb)
            assert (pa._model) is (model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_proofreading_doc_id_resolves_via_registered_active_model(self) -> None:
        """LO passes linguistic ids like '2', not RuntimeUID — cache must load via model= binding."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_persistence import DocumentPersistence

        ctx = MagicMock()
        model = MagicMock()
        cached = _v3_payload(
            bad={"fp_cached": [{"s": 0, "l": 3, "g": ["fix"], "c": "c", "f": "f", "r": "wa_g_rule||test"}]},
        )
        gp.grammar_registry.doc_persistence_instances.clear()
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = DocumentPersistence(ctx, "2", model=model)
                hit = dp.get("fp_cached")
            assert (hit) is not None
            assert (len(hit)) == (1)
            assert (dp._model) is (model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_lazy_bind_loads_udprops_after_init_without_model(self) -> None:
        """Cache embedded in the ODT must load once get_persistence(..., model=) binds the doc."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = _v3_payload(
            bad={"fp_cached": [{"s": 0, "l": 3, "g": ["fix"], "c": "c", "f": "f", "r": "wa_g_rule||test"}]},
        )
        gp.grammar_registry.doc_persistence_instances.clear()
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "2")
                gp.grammar_registry.doc_persistence_instances["2"] = dp
                assert (dp._model) is None
                gp.get_persistence(ctx, "2", model=model)
                hit = dp.get("fp_cached")

            assert (hit) is not None
            assert (len(hit)) == (1)
            assert (hit[0]["n_error_start"]) == (0)
            assert (dp._model) is (model)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_document_persistence_persist_prunes_to_session(self) -> None:
        from plugin.writer.locale.grammar_persistence import DocumentPersistence

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            dp = DocumentPersistence(ctx, "doc-x", model=model)
        assert (dp.get("fp_missing")) is None
        dp.put("fp1", "en-US", [{"n_error_start": 0, "n_error_length": 1}])
        dp.put("fp2", "en-US", [])
        dp.get("fp1")
        with patch("plugin.doc.udprops.set_document_property") as mock_set:
            dp._persist_to_udprops()
        assert (mock_set.called)
        args = mock_set.call_args[0]
        assert (args[0]) is (model)
        written = json.loads(str(args[2]))
        assert (written.get("version")) == (GRAMMAR_CACHE_VERSION)
        assert ("model") in (written)
        en = written["locales"]["en-US"]
        assert ("fp1") in (en.get("bad", {}))
        assert ("fp2") in (en.get("good", []))
        assert (en["bad"]["fp1"][0]["s"]) == (0)

    def test_document_event_on_save_triggers_persist(self) -> None:
        """documentEventOccured with OnSave should drive set_document_property."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-save", model=model)

        dp.put("fp_save", "en-US", [])
        listener = gp._GrammarDocumentEventListener(dp)
        save_event = MagicMock()
        save_event.EventName = "OnSave"

        with patch("plugin.doc.udprops.set_document_property") as mock_set:
            listener.documentEventOccured(save_event)

        assert (mock_set.called), "OnSave must call set_document_property"
        args = mock_set.call_args[0]
        assert (args[0]) is (model)
        written = json.loads(str(args[2]))
        assert ("fp_save") in (written["locales"]["en-US"].get("good", []))

    def test_document_event_on_unload_triggers_teardown(self) -> None:
        """documentEventOccured with OnUnload should teardown and clear the cache."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-unload", model=model)

        dp.put("fp_x", "en-US", [])
        assert (dp.get("fp_x")) is not None

        listener = gp._GrammarDocumentEventListener(dp)
        unload_event = MagicMock()
        unload_event.EventName = "OnUnload"
        listener.documentEventOccured(unload_event)

        assert (dp._teardown_done)
        assert (dp.get("fp_x")) is None

    def test_document_event_on_unload_clears_locales_cache(self) -> None:
        """teardown must clear the document from doc_locales."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-locales", model=model)

        gp.grammar_registry.record_enqueued_locale("doc-locales", "en-US")
        gp.grammar_registry.doc_persistence_instances["doc-locales"] = dp

        listener = gp._GrammarDocumentEventListener(dp)
        unload_event = MagicMock()
        unload_event.EventName = "OnUnload"
        listener.documentEventOccured(unload_event)

        assert dp._teardown_done
        assert "doc-locales" not in gp.grammar_registry.doc_locales

    def test_document_event_listener_disposing_triggers_teardown(self) -> None:
        """The single combined listener also handles broadcaster ``disposing``."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            dp = gp.DocumentPersistence(ctx, "doc-disp", model=model)

        listener = gp._GrammarDocumentEventListener(dp)
        listener.disposing(MagicMock())
        assert (dp._teardown_done)

    def test_document_event_listener_has_correct_uno_method_names(self) -> None:
        """Guardrail: the listener must expose UNO IDL method names exactly."""
        from plugin.writer.locale import grammar_persistence as gp

        assert (hasattr(gp._GrammarDocumentEventListener, "documentEventOccured"))
        assert (hasattr(gp._GrammarDocumentEventListener, "disposing"))
        assert not (hasattr(gp._GrammarDocumentEventListener, "documentEvent")), "stale typo ``documentEvent`` must not be present"

    def test_document_persistence_entries_independent_of_sentence_cache(self) -> None:
        """DocumentPersistence._entries stores loaded/persisted data without touching grammar_registry.sentence_cache."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = _v3_payload(
            good=["fp_clean"],
            bad={"fp_err": [{"s": 0, "l": 4, "g": ["test"], "c": "c", "f": "f", "r": "wa_g_rule||test"}]},
        )
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-entries-test", model=model)

            # Check that _entries was populated
            assert (len(dp._entries)) == (2)
            assert (dp._entries["fp_clean"]) == ({"": []})
            assert (len(dp._entries["fp_err"][""])) == (1)

            # Check that grammar_registry.sentence_cache was NOT populated by _load_from_udprops
            assert (len(gp.grammar_registry.sentence_cache)) == (0)

            # Check that get() returns from _entries and doesn't pollute sentence_cache
            hit_clean = dp.get("fp_clean")
            assert (hit_clean) == ([])
            hit_err = dp.get("fp_err")
            assert (hit_err) is not None
            assert (len(hit_err)) == (1)
            assert (len(gp.grammar_registry.sentence_cache)) == (0)

            # Check put() updates _entries without touching sentence_cache
            dp.put("fp_new", "en-US", [{"n_error_start": 2, "n_error_length": 3}])
            assert ("fp_new") in (dp._entries)
            assert (len(gp.grammar_registry.sentence_cache)) == (0)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_sniff_v2_cache_identity(self) -> None:
        from plugin.writer.locale.grammar_persistence import sniff_v2_cache_identity

        assert (sniff_v2_cache_identity({"bad": {"fp": [{"r": "harper||SpellCheck"}]}})) == ("harper")
        assert (sniff_v2_cache_identity({"bad": {"fp": [{"r": "wa_g_rule||comma"}]}})) == ("llm")
        assert (sniff_v2_cache_identity({"good": ["fp"], "bad": {}})) is None
        assert (sniff_v2_cache_identity(
                {"bad": {"a": [{"r": "harper||X"}], "b": [{"r": "wa_g_rule||Y"}]}}
            )) is None

    def test_v2_llm_adopts_first_incoming_model(self) -> None:
        """v2 LLM rows keep serving after upgrade: first llm: identity owns them."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = {
            "version": 2,
            "good": ["fp_clean"],
            "bad": {"fp_err": [{"s": 0, "l": 4, "g": ["x"], "c": "c", "f": "f", "r": "wa_g_rule||test"}]},
        }
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-v2-llm", model=model)
            assert (len(dp._entries)) == (2)
            assert (dp.get("fp_err")) is None
            dp.ensure_identity("llm:gpt-upgrade")
            hit = dp.get("fp_err")
            assert (hit) is not None
            assert (len(hit)) == (1)
            assert (dp._blob_identity) == ("llm:gpt-upgrade")
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_v2_good_only_dropped(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = {"version": 2, "good": ["fp_clean"], "bad": {}}
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-v2-good", model=model)
            assert (len(dp._entries)) == (0)
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_v3_model_mismatch_skips_entries(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = _v3_payload(good=["fp_clean"])
        cached["model"] = "harper"
        cached["ignored_rules"] = ["old-harper-rule"]
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-mismatch", model=model)
            assert (len(dp._entries)) == (0)
            assert (dp._ignored_rules) == (set())
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_v3_legacy_row_adopts_first_locale_only(self) -> None:
        """A locale-blind v3 row is not served to a second CharLocale."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        cached = _v3_payload(
            bad={"fp_same": [{"s": 0, "l": 3, "g": ["fix"], "c": "c", "f": "f", "r": "wa_g_rule||en"}]},
        )
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-legacy-locale", model=model)
            # A non-adopting probe must not bind the blind row to a locale.
            assert (dp.get("fp_same", "en-US", adopt_legacy=False)) is None
            en_hit = dp.get("fp_same", "en-US")
            fr_hit = dp.get("fp_same", "fr-FR")
            assert (dp.get("fp_same", "en-US", adopt_legacy=False)) is not None
            assert (en_hit) is not None
            assert (en_hit[0]["rule_identifier"]) == ("wa_g_rule||en")
            assert (fr_hit) is None
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_v4_round_trip_keeps_locales_apart(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=None):
                dp = gp.DocumentPersistence(ctx, "doc-v4", model=model)
            dp.put("fp_same", "en-US", [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "en"}])
            dp.put("fp_same", "fr-FR", [])
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.set_document_property") as mock_set:
                dp._persist_to_udprops()
            raw = str(mock_set.call_args[0][2])
            written = json.loads(raw)
            assert (written["version"]) == (GRAMMAR_CACHE_VERSION)
            assert ("fp_same") in (written["locales"]["en-US"]["bad"])
            assert ("fp_same") in (written["locales"]["fr-FR"]["good"])
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=raw):
                dp2 = gp.DocumentPersistence(ctx, "doc-v4-load", model=model)
            en_hit = dp2.get("fp_same", "en-US")
            fr_hit = dp2.get("fp_same", "fr-FR")
            assert (en_hit) is not None
            assert (en_hit[0]["rule_identifier"]) == ("en")
            assert (fr_hit) == ([])
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_identity_switch_drops_loaded_ignored_rules(self) -> None:
        """A checker switch clears ignore ids that were valid for the old identity."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        matched = _v3_payload(good=["fp_clean"])
        matched["ignored_rules"] = ["old-rule"]
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(matched)):
                dp = gp.DocumentPersistence(ctx, "doc-ignore-switch", model=model)
            assert ("old-rule") in (dp._ignored_rules)
            dp.ensure_identity(_TEST_IDENT)
            assert ("old-rule") in (dp._ignored_rules)
            dp.ensure_identity("harper")
            assert (dp._ignored_rules) == (set())
            assert (dp._entries) == ({})
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="harper"), \
                 patch("plugin.doc.udprops.set_document_property") as mock_set:
                dp._persist_to_udprops()
            written = json.loads(str(mock_set.call_args[0][2]))
            assert (written["ignored_rules"]) == ([])
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_apply_language_change_paragraph_relative(self) -> None:
        """Language change must update char locale via a paragraph-relative cursor offset."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_worker import apply_language_change

        ctx = MagicMock()
        model = MagicMock()
        ctrl = MagicMock()
        view_cursor = MagicMock()
        view_start = MagicMock()
        text_obj = MagicMock()
        doc_cursor = MagicMock()

        gp.grammar_registry.doc_persistence_instances["test-doc"] = gp.DocumentPersistence(ctx, "test-doc", model=model)
        model.getCurrentController.return_value = ctrl
        ctrl.getViewCursor.return_value = view_cursor
        view_cursor.getStart.return_value = view_start

        model.getText.return_value = text_obj
        text_obj.createTextCursorByRange.return_value = doc_cursor

        doc_cursor.gotoStartOfParagraph = MagicMock(return_value=True)
        doc_cursor.goRight = MagicMock(return_value=True)
        doc_cursor.getString.return_value = "Hola."

        # Test start_pos=5
        apply_language_change(ctx, "test-doc", "Hola.", "es-ES", start_pos=5)

        text_obj.createTextCursorByRange.assert_called_once_with(view_start)
        doc_cursor.gotoStartOfParagraph.assert_called_once_with(False)
        assert doc_cursor.goRight.call_count == 2
        doc_cursor.goRight.assert_any_call(5, False)
        doc_cursor.goRight.assert_any_call(5, True)

        doc_cursor.setPropertyValue.assert_called_once()
        args, _ = doc_cursor.setPropertyValue.call_args
        assert args[0] == "CharLocale"
        # Language/Country are dynamically set on mock object so assert they were accessed correctly or verify the struct setup
        # The struct creation returns a mock, so we can verify properties weren't None.
        assert args[1] is not None

    def test_apply_language_change_zero_offset_is_paragraph_start(self) -> None:
        """start_pos 0 is the first sentence, not findNext from the caret."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_worker import apply_language_change

        ctx = MagicMock()
        model = MagicMock()
        ctrl = MagicMock()
        view_cursor = MagicMock()
        view_start = MagicMock()
        text_obj = MagicMock()
        doc_cursor = MagicMock()

        gp.grammar_registry.doc_persistence_instances["test-doc-zero"] = gp.DocumentPersistence(ctx, "test-doc-zero", model=model)
        try:
            model.getCurrentController.return_value = ctrl
            ctrl.getViewCursor.return_value = view_cursor
            view_cursor.getStart.return_value = view_start
            model.getText.return_value = text_obj
            text_obj.createTextCursorByRange.return_value = doc_cursor
            doc_cursor.gotoStartOfParagraph = MagicMock(return_value=True)
            doc_cursor.goRight = MagicMock(return_value=True)
            doc_cursor.getString.return_value = "Hola."

            apply_language_change(ctx, "test-doc-zero", "Hola.", "es-ES", start_pos=0)

            text_obj.createTextCursorByRange.assert_called_once_with(view_start)
            doc_cursor.gotoStartOfParagraph.assert_called_once_with(False)
            doc_cursor.goRight.assert_called_once_with(5, True)
            doc_cursor.setPropertyValue.assert_called_once()
            assert doc_cursor.setPropertyValue.call_args.args[0] == "CharLocale"
            model.findNext.assert_not_called()
        finally:
            gp.grammar_registry.doc_persistence_instances.pop("test-doc-zero", None)

    def test_apply_language_change_zero_offset_skips_caret_search_on_mismatch(self) -> None:
        """A miss calls neither findFirst nor findNext and writes nothing."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_worker import apply_language_change

        ctx = MagicMock()
        model = MagicMock()
        ctrl = MagicMock()
        view_cursor = MagicMock()
        view_start = MagicMock(name="view_start")
        text_obj = MagicMock()
        doc_cursor = MagicMock()

        gp.grammar_registry.doc_persistence_instances["test-doc-miss"] = gp.DocumentPersistence(ctx, "test-doc-miss", model=model)
        try:
            model.getCurrentController.return_value = ctrl
            ctrl.getViewCursor.return_value = view_cursor
            view_cursor.getStart.return_value = view_start
            model.getText.return_value = text_obj
            text_obj.createTextCursorByRange.return_value = doc_cursor
            doc_cursor.gotoStartOfParagraph = MagicMock(return_value=True)
            doc_cursor.goRight = MagicMock(return_value=True)
            doc_cursor.getString.return_value = "Other."
            model.findAll.return_value = None

            res = apply_language_change(ctx, "test-doc-miss", "Hola.", "es-ES", start_pos=0)

            assert res is False
            model.findNext.assert_not_called()
            model.findFirst.assert_not_called()
            doc_cursor.setPropertyValue.assert_not_called()
        finally:
            gp.grammar_registry.doc_persistence_instances.pop("test-doc-miss", None)

    def test_apply_language_change_two_findall_hits_same_paragraph_writes_nothing(self) -> None:
        """Two findAll hits with the same paragraph text write nothing and return False."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_worker import apply_language_change

        ctx = MagicMock()
        model = MagicMock()
        ctrl = MagicMock()
        view_cursor = MagicMock()
        text_obj = MagicMock()
        doc_cursor = MagicMock()

        gp.grammar_registry.doc_persistence_instances["test-doc-ambig"] = gp.DocumentPersistence(ctx, "test-doc-ambig", model=model)
        try:
            model.getCurrentController.return_value = ctrl
            ctrl.getViewCursor.return_value = view_cursor
            model.getText.return_value = text_obj
            text_obj.createTextCursorByRange.return_value = doc_cursor
            doc_cursor.gotoStartOfParagraph = MagicMock(return_value=True)
            doc_cursor.goRight = MagicMock(return_value=True)
            doc_cursor.getString.return_value = "Mismatched."

            hit1 = MagicMock()
            hit2 = MagicMock()
            hit1.getText.return_value = text_obj
            hit2.getText.return_value = text_obj
            matches = MagicMock()
            matches.getCount.return_value = 2
            matches.getByIndex.side_effect = [hit1, hit2]
            model.findAll.return_value = matches

            para_cursor = MagicMock()
            para_cursor.getString.return_value = "Target paragraph."
            text_obj.createTextCursorByRange.side_effect = [doc_cursor, para_cursor, para_cursor]

            res = apply_language_change(ctx, "test-doc-ambig", "Sentence.", "es-ES", start_pos=0, paragraph_text="Target paragraph.")
            assert res is False
            hit1.setPropertyValue.assert_not_called()
            hit2.setPropertyValue.assert_not_called()
        finally:
            gp.grammar_registry.doc_persistence_instances.pop("test-doc-ambig", None)

    def test_apply_language_change_one_findall_hit_matching_paragraph_writes_range(self) -> None:
        """One findAll hit whose paragraph equals paragraph_text writes that range."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_worker import apply_language_change

        ctx = MagicMock()
        model = MagicMock()
        ctrl = MagicMock()
        view_cursor = MagicMock()
        text_obj = MagicMock()
        doc_cursor = MagicMock()

        gp.grammar_registry.doc_persistence_instances["test-doc-one"] = gp.DocumentPersistence(ctx, "test-doc-one", model=model)
        try:
            model.getCurrentController.return_value = ctrl
            ctrl.getViewCursor.return_value = view_cursor
            model.getText.return_value = text_obj
            text_obj.createTextCursorByRange.return_value = doc_cursor
            doc_cursor.gotoStartOfParagraph = MagicMock(return_value=True)
            doc_cursor.goRight = MagicMock(return_value=True)
            doc_cursor.getString.return_value = "Mismatched."

            hit1 = MagicMock()
            hit2 = MagicMock()
            hit1.getText.return_value = text_obj
            hit2.getText.return_value = text_obj
            matches = MagicMock()
            matches.getCount.return_value = 2
            matches.getByIndex.side_effect = [hit1, hit2]
            model.findAll.return_value = matches

            tc1 = MagicMock()
            tc1.getString.return_value = "Target paragraph."
            tc2 = MagicMock()
            tc2.getString.return_value = "Other paragraph."

            text_obj.createTextCursorByRange.side_effect = [doc_cursor, tc1, tc2]

            res = apply_language_change(ctx, "test-doc-one", "Sentence.", "es-ES", start_pos=0, paragraph_text="Target paragraph.")
            assert res is True
            hit1.setPropertyValue.assert_called_once()
            assert hit1.setPropertyValue.call_args[0][0] == "CharLocale"
            hit2.setPropertyValue.assert_not_called()
        finally:
            gp.grammar_registry.doc_persistence_instances.pop("test-doc-one", None)

    def test_second_bind_does_not_replace_model_or_reload(self) -> None:
        """A later model= must not swap the Writer or read udprops over live edits."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        first = MagicMock()
        second = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.doc.udprops.get_document_property", return_value=None) as mock_get:
                dp = gp.get_persistence(ctx, "2", model=first)
                assert dp is not None
                assert dp._model is first
                loads = mock_get.call_count
                again = gp.get_persistence(ctx, "2", model=second)
                assert again is dp
                assert dp._model is first
                assert mock_get.call_count == loads
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_unknown_cache_version_clears(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        future = {
            "version": 99,
            "model": _TEST_IDENT,
            "locales": {"en-US": {"good": ["fp_future"], "bad": {}}},
            "ignored_rules": ["future-rule"],
        }
        non_string_model = _v3_payload(good=["fp_clean"])
        non_string_model["model"] = ["not", "a", "string"]
        non_string_model["ignored_rules"] = ["bad-model-rule"]
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(future)):
                dp = gp.DocumentPersistence(ctx, "doc-future", model=model)
            assert (dp._entries) == ({})
            assert (dp._ignored_rules) == (set())
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(non_string_model)):
                dp_bad = gp.DocumentPersistence(ctx, "doc-bad-model", model=model)
            assert (dp_bad._entries) == ({})
            assert (dp_bad._ignored_rules) == (set())
        finally:
            gp.clear_all_document_persistence(ctx)

    def test_concurrent_get_persistence_single_instance_and_listener(self) -> None:
        """Two threads calling get_persistence concurrently produce one instance and listener."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        listeners: list[object] = []
        model.addDocumentEventListener.side_effect = listeners.append
        barrier = threading.Barrier(2)

        orig_load = gp.DocumentPersistence._load_from_udprops

        def _slow_load(self_dp: Any) -> None:
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            orig_load(self_dp)  # pyright: ignore[reportCallIssue,reportArgumentType]

        gp.grammar_registry.clear_all(ctx)
        try:
            results: list[object] = []

            def _thread_worker(with_model: bool) -> None:
                m = model if with_model else None
                dp = gp.get_persistence(ctx, "doc-race", model=m)
                results.append(dp)

            with patch("plugin.writer.locale.grammar_persistence._HAVE_UNO_DOC_EVENTS", True), \
                 patch.object(gp.DocumentPersistence, "_load_from_udprops", _slow_load):
                t1 = threading.Thread(target=_thread_worker, args=(True,))
                t2 = threading.Thread(target=_thread_worker, args=(True,))
                t1.start()
                t2.start()
                t1.join(timeout=3.0)
                t2.join(timeout=3.0)

            assert len(gp.grammar_registry.doc_persistence_instances) == 1
            assert len(results) == 2
            assert results[0] is results[1]
            assert len(listeners) == 1
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_teardown_orphan_does_not_remove_winner(self) -> None:
        """An orphan persistence instance calling _teardown does not remove the winning instance."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence._HAVE_UNO_DOC_EVENTS", True):
                winner = gp.get_persistence(ctx, "doc-orphan", model=model)
                assert winner is not None
                assert gp.grammar_registry.doc_persistence_instances.get("doc-orphan") is winner

                orphan = gp.DocumentPersistence(ctx, "doc-orphan")
                orphan._teardown()

                assert gp.grammar_registry.doc_persistence_instances.get("doc-orphan") is winner
                assert not winner._teardown_done
                assert winner._doc_listener is not None
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_late_bind_preserves_live_session_rows(self) -> None:
        """A late bind merges missing blob rows without overwriting live session rows."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            dp = gp.DocumentPersistence(ctx, "doc-late")
            dp.put("fp_live", "en-US", [])

            blob = {
                "version": 4,
                "model": "test-checker",
                "locales": {"en-US": {"good": ["fp_blob"], "bad": {}}},
            }
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(blob)), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp._bind_model(model)

            assert dp.get("fp_live", "en-US") == []
            assert dp.get("fp_blob", "en-US") == []
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_save_compresses_only_accessed_rows(self) -> None:
        """Save must compress only session-accessed rows outside the lock."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            dp = gp.DocumentPersistence(ctx, "doc-compress", model=model)
            dp._entries["fp_unaccessed"] = {"en-US": [{"s": 0, "l": 2, "g": ["a"]}]}
            dp.put("fp_accessed", "en-US", [{"s": 3, "l": 4, "g": ["b"]}])

            with patch("plugin.writer.locale.grammar_persistence.compress_error", side_effect=lambda e: dict(e)) as mock_compress, \
                 patch("plugin.doc.udprops.set_document_property") as mock_set, \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp._persist_to_udprops()

            assert mock_set.called
            assert mock_compress.call_count == 1
            assert mock_compress.call_args[0][0] == {"s": 3, "l": 4, "g": ["b"]}
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_compress_and_decompress_error(self) -> None:
        from plugin.writer.locale import grammar_persistence as gp

        original = {
            "n_error_start": 10,
            "n_error_length": 5,
            "suggestions": ["hello", "hi"],
            "short_comment": "comment",
            "full_comment": "long comment",
            "rule_identifier": "rule1",
            "extra_key": "stays_same",
        }
        compressed = gp.compress_error(original)
        assert compressed == {
            "s": 10,
            "l": 5,
            "g": ["hello", "hi"],
            "c": "comment",
            "f": "long comment",
            "r": "rule1",
            "extra_key": "stays_same",
        }
        decompressed = gp.decompress_error(compressed)
        assert decompressed == original

    def test_load_drops_malformed_rows_and_later_valid_load_replaces(self) -> None:
        """Malformed good, bad, and ignored_rules entries are dropped without raising."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            malformed_blob = {
                "version": 4,
                "model": "test-checker",
                "locales": {
                    "en-US": {
                        "good": [1],
                        "bad": {1: []},
                    }
                },
                "ignored_rules": [{}],
            }
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(malformed_blob)), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp = gp.DocumentPersistence(ctx, "doc-malformed", model=model)

            assert len(dp._entries) == 0
            assert len(dp._ignored_rules) == 0

            valid_blob = {
                "version": 4,
                "model": "test-checker",
                "locales": {
                    "en-US": {
                        "good": ["fp_valid"],
                        "bad": {},
                    }
                },
                "ignored_rules": ["valid_rule"],
            }
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(valid_blob)), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp._load_from_udprops()

            assert "fp_valid" in dp._entries
            assert "valid_rule" in dp._ignored_rules
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_cache_max_chars_bound_on_load_and_save(self) -> None:
        """Raw property over GRAMMAR_CACHE_MAX_CHARS is not loaded; save skips if over."""
        from plugin.writer.locale import grammar_persistence as gp
        from plugin.writer.locale.grammar_proofread_locale import GRAMMAR_CACHE_MAX_CHARS

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            oversize_raw = " " * (GRAMMAR_CACHE_MAX_CHARS + 1)
            with patch("plugin.doc.udprops.get_document_property", return_value=oversize_raw), \
                 patch("json.loads") as mock_loads:
                dp = gp.DocumentPersistence(ctx, "doc-oversize", model=model)
                mock_loads.assert_not_called()
                assert len(dp._entries) == 0

            with patch("plugin.doc.udprops.set_document_property") as mock_set, \
                 patch("json.dumps", return_value="x" * GRAMMAR_CACHE_MAX_CHARS), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test"):
                dp.put("fp1", "en-US", [])
                dp._persist_to_udprops()
                assert mock_set.called

            with patch("plugin.doc.udprops.set_document_property") as mock_set, \
                 patch("json.dumps", return_value="x" * (GRAMMAR_CACHE_MAX_CHARS + 1)), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test"):
                dp._persist_to_udprops()
                mock_set.assert_not_called()
        finally:
            gp.grammar_registry.clear_all(ctx)

    def test_unadopted_v3_legacy_row_omitted_from_save(self) -> None:
        """A v3 legacy row that was never adopted is absent from the saved locales map."""
        from plugin.writer.locale import grammar_persistence as gp

        ctx = MagicMock()
        model = MagicMock()
        gp.grammar_registry.clear_all(ctx)
        try:
            v3_blob = {
                "version": 3,
                "model": "test-checker",
                "good": ["fp_unadopted"],
                "bad": {},
            }
            with patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(v3_blob)), \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp = gp.DocumentPersistence(ctx, "doc-v3", model=model)

            with patch("plugin.doc.udprops.set_document_property") as mock_set, \
                 patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value="test-checker"):
                dp._persist_to_udprops()

            assert mock_set.called
            saved_payload = json.loads(mock_set.call_args[0][2])
            assert saved_payload["locales"] == {}
        finally:
            gp.grammar_registry.clear_all(ctx)


