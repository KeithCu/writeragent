import unittest
from unittest.mock import MagicMock, patch
from plugin.framework.i18n import _, init_i18n, get_lo_locale
import plugin.framework.i18n as i18n_module
import sys

class TestI18n(unittest.TestCase):
    def setUp(self):
        # Reset i18n initialization state
        i18n_module._translation = None

    def test_i18n_fallback(self):
        """Test that untranslated strings return original text."""
        init_i18n()
        self.assertEqual(_("ThisIsAnUntranslatedString999"), "ThisIsAnUntranslatedString999")
        self.assertEqual(_(123), "123")  # Test non-string behavior

    @patch('plugin.framework.config.get_config')
    def test_locale_detection_configured(self, mock_get_config):
        """Test locale detection respects configured language over system."""
        mock_get_config.return_value = "de"
        locale = get_lo_locale()
        self.assertEqual(locale, "de")

    @patch('plugin.framework.config.get_config')
    def test_locale_detection_system_fallback(self, mock_get_config):
        """Test locale detection when configured to 'system' uses UNO."""
        mock_get_config.return_value = "system"

        mock_ctx = MagicMock()
        mock_smgr = MagicMock()
        mock_ctx.getServiceManager.return_value = mock_smgr

        mock_config_provider = MagicMock()
        mock_smgr.createInstanceWithContext.return_value = mock_config_provider

        mock_ca = MagicMock()
        mock_ca.getPropertyValue.return_value = "fr-FR"
        mock_config_provider.createInstanceWithArguments.return_value = mock_ca

        # Mock uno correctly for this test
        mock_uno = MagicMock()
        mock_uno.createUnoStruct.return_value = "mock_struct"

        with patch.dict(sys.modules, {'uno': mock_uno}):
            locale = get_lo_locale(mock_ctx)
            self.assertEqual(locale, "fr_FR")

    @patch('plugin.framework.config.get_config')
    @patch('os.environ.get')
    def test_locale_detection_env_fallback(self, mock_env_get, mock_get_config):
        """Test locale detection falls back to LANG env var if UNO fails."""
        mock_get_config.return_value = None
        mock_env_get.return_value = "es_ES.UTF-8"

        mock_ctx = MagicMock()
        mock_ctx.getServiceManager.side_effect = Exception("No UNO")

        locale = get_lo_locale(mock_ctx)
        self.assertEqual(locale, "es_ES")

if __name__ == '__main__':
    unittest.main()
