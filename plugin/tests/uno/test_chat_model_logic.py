import sys
from plugin.framework.path_utils import get_plugin_dir
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock uno and unohelper before importing chat_panel
class BaseStub: pass
class MockUnoBase(BaseStub): pass
class XActionListener(BaseStub): pass
class XTextListener(BaseStub): pass
class XWindowListener(BaseStub): pass
class XItemListener(BaseStub): pass
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
com.sun.star.awt.XTextListener = XTextListener
com.sun.star.awt.XWindowListener = XWindowListener
com.sun.star.awt.XItemListener = XItemListener
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
sys.path.insert(0, get_plugin_dir())

from plugin.modules.chatbot.panel_factory import SendButtonListener
from plugin.framework.dialogs import set_control_text, get_control_text

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
    @patch('plugin.framework.config.get_config')
    @patch('plugin.framework.config.set_config')
    @patch('plugin.modules.chatbot.tool_loop.update_lru_history')
    @patch('plugin.modules.chatbot.tool_loop.get_current_endpoint')
    @patch('plugin.modules.http.client.LlmClient')
    def test_do_send_updates_model(self, mock_llm_client, mock_get_current_endpoint, mock_update_lru, mock_set_config, mock_get_config, mock_ensure_path, *args, **kwargs):
        set_control_text(self.query_control, "Hello AI")
        self.model_selector.getText.return_value = "new-model-xyz"
        mock_get_config.side_effect = lambda ctx, key, default=None: default
        mock_get_current_endpoint.return_value = "http://x"
        
        doc_mock = MagicMock(spec=["getText"])
        with patch.object(self.listener, '_get_document_model', return_value=doc_mock),              patch('plugin.framework.config.get_api_config', MagicMock(return_value={"model": "test", "endpoint": "http://x"})):

            with patch('sys.modules', dict(sys.modules)):
                sys.modules['plugin.main'] = MagicMock()
                self.listener._do_send_chat_with_tools("Hello AI", doc_mock, "writer")

            mock_set_config.assert_any_call(self.ctx, "text_model", "new-model-xyz")
            mock_update_lru.assert_any_call(self.ctx, "new-model-xyz", "model_lru", "http://x")

    @patch('plugin.modules.chatbot.panel_factory._ensure_extension_on_path')
    @patch('plugin.framework.config.get_config')
    @patch('plugin.framework.config.set_config')
    @patch('plugin.framework.config.update_lru_history')
    @patch('plugin.framework.config.get_current_endpoint')
    @patch('plugin.modules.http.client.LlmClient')
    def test_image_model_updates(self, mock_llm_client, mock_get_current_endpoint, mock_update_lru, mock_set_config, mock_get_config, mock_ensure_path, *args, **kwargs):
        set_control_text(self.query_control, "Hello AI")
        self.model_selector.getText.return_value = "new-model-xyz"
        self.image_model_selector.getText.return_value = "new-image-model-xyz"
        mock_get_config.side_effect = lambda ctx, key, default=None: default
        mock_get_current_endpoint.return_value = "http://x"

        doc_mock = MagicMock(spec=["getText"])
        with patch.object(self.listener, '_get_document_model', return_value=doc_mock),              patch('plugin.framework.config.get_api_config', MagicMock(return_value={"model": "test", "endpoint": "http://x"})):

            with patch('sys.modules', dict(sys.modules)):
                sys.modules['plugin.main'] = MagicMock()
                self.listener._do_send_chat_with_tools("Hello AI", doc_mock, "writer")

            mock_set_config.assert_any_call(self.ctx, "image_model", "new-image-model-xyz")
            mock_update_lru.assert_any_call(self.ctx, "new-image-model-xyz", "image_model_lru", "http://x")

    @patch('plugin.framework.logging.update_activity_state')
    def test_doc_type_leakage(self, mock_update_activity):
        self.listener.initial_doc_type = "Writer"

        # Mock _get_document_model to return a Calc document (getSheets instead of getText)
        doc_mock = MagicMock()
        self.set_control_text(listener.response_control, "")

        # We need to correctly patch the checks used in _do_send to identify the document
        with patch.object(self.listener, '_get_document_model', return_value=doc_mock),              patch('plugin.framework.document.is_calc', return_value=True),              patch('plugin.framework.document.is_writer', return_value=False),              patch('plugin.framework.document.is_draw', return_value=False):
            self.listener._do_send()

            # Since document changed from Writer to Calc, it should abort and show an error.
            self.assertEqual(self.listener._terminal_status, "Error")
            # Verify the response control text was updated with the error
            self.assertTrue("[Internal Error: Document type changed from Writer to Calc! Please file an error.]" in self.get_control_text(listener.response_control))

    @patch('plugin.framework.logging.update_activity_state')
    def test_button_lifecycle(self, mock_update_activity):
        # We need to test the actionPerformed method where _set_button_states is called.
        # Let's mock _do_send to raise an Exception to test the exception path.

        self.listener._do_send = MagicMock(side_effect=Exception("Test Error"))

        # Call actionPerformed
        evt = MagicMock()
        self.listener.actionPerformed(evt)

        # It should enable send and disable stop in the finally block
        self.assertEqual(self.listener.send_control.getModel().Enabled, True)
        self.assertEqual(self.listener.stop_control.getModel().Enabled, False)
        # And send_busy should be reset
        self.assertFalse(self.listener._send_busy)
if __name__ == '__main__':
    unittest.main()
