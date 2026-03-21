import unittest
from gettext import NullTranslations
from unittest.mock import MagicMock, patch
from plugin.framework.i18n import _, get_lo_locale
import plugin.framework.i18n as i18n_module
import sys

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

if __name__ == '__main__':
    unittest.main()
