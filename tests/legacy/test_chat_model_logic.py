import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock uno and unohelper before importing chat_panel
class MockUnoBase: pass
class XActionListener: pass
class XUIElement: pass
class XToolPanel: pass
class XSidebarPanel: pass
class XUIElementFactory: pass
class XTextComponent: pass

sys.modules['uno'] = MagicMock()
mock_unohelper = MagicMock()
mock_unohelper.Base = MockUnoBase
sys.modules['unohelper'] = mock_unohelper

# Mock com structure
com = MagicMock()
com.sun.star.awt.XActionListener = XActionListener
com.sun.star.ui.XUIElement = XUIElement
com.sun.star.ui.XToolPanel = XToolPanel
com.sun.star.ui.XSidebarPanel = XSidebarPanel
com.sun.star.ui.XUIElementFactory = XUIElementFactory
com.sun.star.awt.XTextComponent = XTextComponent
sys.modules['com'] = com
sys.modules['com.sun.star'] = com.sun.star
sys.modules['com.sun.star.ui'] = com.sun.star.ui
sys.modules['com.sun.star.ui.UIElementType'] = com.sun.star.ui.UIElementType
sys.modules['com.sun.star.awt'] = com.sun.star.awt
sys.modules['com.sun.star.task'] = com.sun.star.task

# Set up specific constants if needed
com.sun.star.ui.UIElementType.TOOLPANEL = 1

# Mock core modules that chat_panel depends on
sys.modules['core'] = MagicMock()
sys.modules['core.logging'] = MagicMock()
sys.modules['core.async_stream'] = MagicMock()
sys.modules['core.config'] = MagicMock()
sys.modules['core.api'] = MagicMock()
sys.modules['core.document'] = MagicMock()
sys.modules['core.document_tools'] = MagicMock()
sys.modules['core.constants'] = MagicMock()

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from plugin.modules.chatbot.panel_factory import SendButtonListener

class TestChatModelLogic(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.frame = MagicMock()
        self.send_control = MagicMock()
        self.stop_control = MagicMock()
        self.query_control = MagicMock()
        self.response_control = MagicMock()
        self.image_model_selector = MagicMock()
        self.model_selector = MagicMock()
        self.status_control = MagicMock()
        self.session = MagicMock()
        self.session.messages = [{"role": "system", "content": "test"}]

        self.listener = SendButtonListener(
            self.ctx, self.frame, self.send_control, self.stop_control,
            self.query_control, self.response_control, self.image_model_selector,
            self.model_selector, self.status_control, self.session
        )

    @patch('plugin.modules.chatbot.panel_factory._ensure_extension_on_path')
    @patch('plugin.modules.core.config.get_config')
    @patch('plugin.modules.core.config.set_config')
    @patch('plugin.modules.core.config.update_lru_history')
    @patch('plugin.framework.http.LlmClient')
    def test_do_send_updates_model(self, mock_llm_client, mock_update_lru, mock_set_config, mock_get_config, mock_ensure_path):
        # Setup mocks
        self.query_control.getModel().Text = "Hello AI"
        self.model_selector.getText.return_value = "new-model-xyz"
        mock_get_config.side_effect = lambda ctx, key, default: default
        
        # Mock _get_document_model (Writer doc: has getText, no getSheets) so we reach the model update
        doc_mock = MagicMock(spec=["getText"])
        with patch.object(self.listener, '_get_document_model', return_value=doc_mock), \
             patch('plugin.modules.core.config.get_api_config', MagicMock(return_value={"model": "test", "endpoint": "http://x"})):
            
            # This will still fail because of other internal imports in _do_send
            # but we can try to trigger the model update part at least.
            try:
                # Mocking minimal parts to get to model update
                self.listener._do_send()
            except Exception:
                # Expected to fail later in _do_send due to missing document model etc.
                pass

            # Check if set_config was called with the new text model
            mock_set_config.assert_any_call(self.ctx, "text_model", "new-model-xyz")
            mock_update_lru.assert_any_call(self.ctx, "new-model-xyz", "model_lru")

if __name__ == '__main__':
    unittest.main()
