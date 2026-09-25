# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from plugin.writer.locale.grammar_persistence import GRAMMAR_CACHE_VERSION

_TEST_IDENT = "llm:test"


def _v3_payload(*, good: list | None = None, bad: dict | None = None) -> dict:
    return {
        "version": GRAMMAR_CACHE_VERSION,
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
        assert ("fp1") in (written.get("bad", {}))
        assert ("fp2") in (written.get("good", []))
        assert (written["bad"]["fp1"][0]["s"]) == (0)

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
        assert ("fp_save") in (written.get("good", []))

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
            assert (dp._entries["fp_clean"]) == ([])
            assert (len(dp._entries["fp_err"])) == (1)

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
        gp.grammar_registry.clear_all(ctx)
        try:
            with patch("plugin.writer.locale.grammar_persistence.grammar_checker_identity", return_value=_TEST_IDENT), \
                 patch("plugin.doc.udprops.get_document_property", return_value=json.dumps(cached)):
                dp = gp.DocumentPersistence(ctx, "doc-mismatch", model=model)
            assert (len(dp._entries)) == (0)
        finally:
            gp.clear_all_document_persistence(ctx)


