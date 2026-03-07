import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock uno and unohelper
sys.modules['uno'] = MagicMock()
mock_unohelper = MagicMock()
class MockUnoBase: pass
mock_unohelper.Base = MockUnoBase
sys.modules['unohelper'] = mock_unohelper

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from plugin.framework.config import get_image_model, set_image_model, get_config, set_config

class TestConfigSync(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}
        
        # Mock get_config and set_config
        def mock_get_config(ctx, key, default):
            return self.config_data.get(key, default)
        
        def mock_set_config(ctx, key, value):
            self.config_data[key] = value
            
        self.get_patcher = patch('core.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('core.config.set_config', side_effect=mock_set_config)
        self.notify_patcher = patch('core.config.notify_config_changed')
        
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()
        self.mock_notify = self.notify_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()
        self.notify_patcher.stop()

    def test_set_image_model_aihorde(self):
        self.config_data["image_provider"] = "aihorde"
        set_image_model(self.ctx, "new-horde-model")
        
        self.assertEqual(self.config_data.get("aihorde_model"), "new-horde-model")
        self.assertIsNone(self.config_data.get("image_model"))
        self.mock_notify.assert_called_once_with(self.ctx)

    def test_set_image_model_endpoint(self):
        self.config_data["image_provider"] = "endpoint"
        with patch('core.config.update_lru_history') as mock_lru:
            set_image_model(self.ctx, "new-endpoint-model")
            
            self.assertEqual(self.config_data.get("image_model"), "new-endpoint-model")
            self.assertIsNone(self.config_data.get("aihorde_model"))
            mock_lru.assert_called_once_with(self.ctx, "new-endpoint-model", "image_model_lru")
            self.mock_notify.assert_called_once_with(self.ctx)

    def test_get_image_model(self):
        # Test AI Horde
        self.config_data["image_provider"] = "aihorde"
        self.config_data["aihorde_model"] = "horde-1"
        self.assertEqual(get_image_model(self.ctx), "horde-1")
        
        # Test Endpoint
        self.config_data["image_provider"] = "endpoint"
        self.config_data["image_model"] = "end-1"
        self.assertEqual(get_image_model(self.ctx), "end-1")

if __name__ == '__main__':
    unittest.main()
