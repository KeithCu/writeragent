import os
import unittest
import gettext
from gettext import NullTranslations
from unittest.mock import MagicMock, patch
from plugin.framework.i18n import _, get_lo_locale
import plugin.framework.i18n as i18n_module
import sys

from plugin.framework.config import WriterAgentConfig, _build_validated_config_export

# PO-header junk mistakenly saved into config via gettext/translation bugs (i18n + load path)
PO_JUNK = "Project-Id-Version: WriterAgent 1.0\nReport-Msgid-Bugs-To: x\n"


class TestI18n(unittest.TestCase):
    def setUp(self):
        # Reset i18n initialization state
        i18n_module._translation = None

    def test_i18n_fallback(self):
        """Test that gettext with no catalog returns msgid (and non-str is coerced)."""
        i18n_module._translation = NullTranslations()
        self.assertEqual(_("ThisIsAnUntranslatedString999"), "ThisIsAnUntranslatedString999")
        self.assertEqual(_(123), "123")  # Test non-string behavior

    def test_locale_detection_uno(self):
        """Test locale detection uses LibreOffice ooLocale via UNO."""
        mock_ctx = MagicMock()
        mock_smgr = MagicMock()
        mock_ctx.getServiceManager.return_value = mock_smgr

        mock_config_provider = MagicMock()
        mock_smgr.createInstanceWithContext.return_value = mock_config_provider

        mock_ca = MagicMock()
        mock_ca.getPropertyValue.return_value = "fr-FR"
        mock_config_provider.createInstanceWithArguments.return_value = mock_ca

        mock_uno = MagicMock()
        mock_uno.createUnoStruct.return_value = "mock_struct"

        with patch.dict(sys.modules, {'uno': mock_uno}):
            locale = get_lo_locale(mock_ctx)
            self.assertEqual(locale, "fr_FR")

    def test_locale_detection_default_when_uno_fails(self):
        """When UNO/config is unavailable, locale defaults to English (not OS LANG)."""
        mock_ctx = MagicMock()
        mock_ctx.getServiceManager.side_effect = Exception("No UNO")

        locale = get_lo_locale(mock_ctx)
        self.assertEqual(locale, "en_US")

    def test_config_validate_maps_translated_label_to_canonical_in_extra_config(self):
        """Saved UI label (wrong) in dotted key is normalized to schema value via _()."""
        def fake_specs(ctx):
            return [
                {
                    "name": "agent_backend__backend_id",
                    "options": [{"value": "hermes", "label": "Hermes"}],
                }
            ]

        cfg = WriterAgentConfig.from_dict({"endpoint": "http://127.0.0.1:11434"})
        cfg._extra_config["agent_backend.backend_id"] = "GERMAN_HERMES"

        def _fake(msg):
            if msg == "Hermes":
                return "GERMAN_HERMES"
            return msg

        with patch("plugin.framework.settings_dialog.get_settings_field_specs", fake_specs):
            with patch("plugin.framework.i18n._", side_effect=_fake):
                cfg.validate()
        self.assertEqual(cfg._extra_config["agent_backend.backend_id"], "hermes")

    def test_config_validate_maps_translated_label_top_level_field(self):
        """Combo field stored on dataclass (no dots) still normalizes via options."""
        def fake_specs(ctx):
            return [
                {
                    "name": "image_default_aspect",
                    "options": [{"value": "Square", "label": "Square"}],
                }
            ]

        cfg = WriterAgentConfig.from_dict(
            {"endpoint": "http://127.0.0.1:11434", "image_default_aspect": "SQ_LABEL"}
        )

        def _fake(msg):
            if msg == "Square":
                return "SQ_LABEL"
            return msg

        with patch("plugin.framework.settings_dialog.get_settings_field_specs", fake_specs):
            with patch("plugin.framework.i18n._", side_effect=_fake):
                cfg.validate()
        self.assertEqual(cfg.image_default_aspect, "Square")

    def test_config_validate_normalization_noop_when_already_canonical(self):
        """When stored value already matches canonical option value, leave unchanged."""
        def fake_specs(ctx):
            return [
                {
                    "name": "image_default_aspect",
                    "options": [{"value": "Square", "label": "Square"}],
                }
            ]

        cfg = WriterAgentConfig.from_dict(
            {"endpoint": "http://127.0.0.1:11434", "image_default_aspect": "Square"}
        )
        with patch("plugin.framework.settings_dialog.get_settings_field_specs", fake_specs):
            cfg.validate()
        self.assertEqual(cfg.image_default_aspect, "Square")

    def test_po_strip_extra_config_on_validate(self):
        data = {
            "endpoint": "http://localhost:11434",
            "agent_backend.path": PO_JUNK,
            "agent_backend.args": PO_JUNK,
            "agent_backend.acp_agent_name": PO_JUNK,
        }
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        self.assertEqual(cfg._extra_config.get("agent_backend.path"), "")
        self.assertEqual(cfg._extra_config.get("agent_backend.args"), "")
        self.assertEqual(cfg._extra_config.get("agent_backend.acp_agent_name"), "")

    def test_po_strip_seed_to_minus_one(self):
        data = {"endpoint": "http://x", "seed": PO_JUNK}
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        self.assertEqual(cfg.seed, "-1")

    def test_po_strip_top_level_string_field(self):
        data = {"endpoint": "http://x", "additional_instructions": PO_JUNK}
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        self.assertEqual(cfg.additional_instructions, "")

    def test_export_uses_validated_extra_not_raw_json(self):
        """Merged dict for get_config must use cleaned _extra_config, not stale JSON."""
        data = {
            "endpoint": "http://localhost:11434",
            "chat_max_tokens": 16384,
            "agent_backend.path": PO_JUNK,
        }
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        out = _build_validated_config_export(data, cfg)
        self.assertEqual(out["agent_backend.path"], "")
        self.assertNotEqual(out["agent_backend.path"], PO_JUNK)

    def test_export_dataclass_keys_from_attributes(self):
        data = {
            "endpoint": "http://example.com/v1",
            "chat_max_tokens": 2048,
            "agent_backend.path": "",
        }
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        out = _build_validated_config_export(data, cfg)
        self.assertEqual(out["endpoint"], "http://example.com/v1")
        self.assertEqual(out["chat_max_tokens"], 2048)

    def test_extra_key_fallback_when_missing_from_extra_config(self):
        """If a key is absent from _extra_config, keep JSON value (edge case)."""
        data = {"endpoint": "http://x", "orphan.key": "keep-me"}
        cfg = WriterAgentConfig.from_dict(data)
        cfg.validate()
        del cfg._extra_config["orphan.key"]
        out = _build_validated_config_export(data, cfg)
        self.assertEqual(out.get("orphan.key"), "keep-me")

    def test_backend_translation_normalization(self):
        from plugin.modules.agent_backend.registry import normalize_backend_id, get_backend

        self.assertEqual(normalize_backend_id("builtin"), "builtin")
        self.assertEqual(normalize_backend_id("hermes"), "hermes")
        self.assertEqual(normalize_backend_id("claude"), "claude")
        self.assertEqual(normalize_backend_id("Built-in"), "builtin")
        self.assertEqual(normalize_backend_id("Eingebaut"), "builtin")
        self.assertEqual(normalize_backend_id("Hermes"), "hermes")
        self.assertEqual(normalize_backend_id("nonexistent"), "builtin")
        self.assertIsNotNone(get_backend("Eingebaut"))

    def test_i18n_translation_loading(self):
        """gettext can load writeragent.mo and translate 'Built-in' to German."""
        localedir = os.path.join(os.path.abspath("."), "plugin", "locales")
        translation = gettext.translation("writeragent", localedir, languages=["de"], fallback=True)
        self.assertEqual(translation.gettext("Built-in"), "Eingebaut")
        self.assertEqual(translation.gettext("Backend"), "Backend")

    def test_legacy_ui_imports(self):
        """Import legacy_ui with full UNO; otherwise expect ImportError (headless pytest)."""
        try:
            from plugin.framework import legacy_ui
            self.assertIsNotNone(legacy_ui)
        except ImportError as e:
            err = str(e)
            self.assertTrue(
                any(
                    part in err
                    for part in (
                        "unohelper",
                        "uno",
                        "com.sun.star",
                        "com",
                        "XItemListener",
                        "unknown",
                    )
                ),
                f"Unexpected import error: {e!r}",
            )


if __name__ == '__main__':
    unittest.main()
