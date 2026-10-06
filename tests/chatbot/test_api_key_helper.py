import unittest
from plugin.chatbot.settings_dialog import effective_api_key

class TestApiKeyHelper(unittest.TestCase):
    def test_typed_url_no_saved_key_sends_no_key(self):
        # typed URL with no saved key sends no key
        eff = effective_api_key(
            typed_key="old_key",
            saved_endpoint="http://old",
            target_endpoint="http://new",
            saved_key="old_key",
            target_key="",
            user_edited_key=False,
        )
        self.assertEqual(eff, "")

    def test_typed_url_with_saved_key_sends_that_key(self):
        # typed URL with a saved key sends that key
        eff = effective_api_key(
            typed_key="old_key",
            saved_endpoint="http://old",
            target_endpoint="http://new",
            saved_key="old_key",
            target_key="new_key",
            user_edited_key=False,
        )
        self.assertEqual(eff, "new_key")

    def test_user_edited_key_after_url_change(self):
        # user-edited key after URL change is used
        eff = effective_api_key(
            typed_key="custom_key",
            saved_endpoint="http://old",
            target_endpoint="http://new",
            saved_key="old_key",
            target_key="new_key",
            user_edited_key=True,
        )
        self.assertEqual(eff, "custom_key")

    def test_old_endpoints_key_never_sent_to_new_url(self):
        # old endpoint's key is never sent to the new URL
        eff = effective_api_key(
            typed_key="old_key",
            saved_endpoint="http://old",
            target_endpoint="http://new",
            saved_key="old_key",
            target_key="new_key",
            user_edited_key=False,
        )
        self.assertNotEqual(eff, "old_key")
        self.assertEqual(eff, "new_key")

        eff = effective_api_key(
            typed_key="old_key",
            saved_endpoint="http://old",
            target_endpoint="http://new",
            saved_key="old_key",
            target_key="",
            user_edited_key=False,
        )
        self.assertNotEqual(eff, "old_key")
        self.assertEqual(eff, "")


def test_test_connection_uses_endpoint_listener_key():
    """Test Connection sends the key the model fetch would send, not the raw field."""
    from unittest.mock import MagicMock, patch

    from plugin.chatbot.dialog_views import TestConnectionListener

    endpoint_ctrl = MagicMock()
    endpoint_ctrl.getText.return_value = "http://127.0.0.1:8080"
    key_ctrl = MagicMock()
    key_ctrl.getText.return_value = "sk-or-previous-endpoint"
    controls = {"endpoint": endpoint_ctrl, "api_key": key_ctrl}
    sent = []

    listener = TestConnectionListener(MagicMock(), MagicMock(), api_key_override=lambda: "")
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda dlg, name: controls.get(name)), \
         patch("plugin.chatbot.dialog_views.get_control_text", side_effect=lambda c, *a, **k: c.getText()), \
         patch("plugin.chatbot.quick_setup.check_endpoint_connection", side_effect=lambda ep, key: sent.append((ep, key)) or (True, "ok")), \
         patch("plugin.framework.worker_pool.run_in_background", side_effect=lambda fn, name=None: fn()), \
         patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: None):
        listener.on_action_performed(None)
    assert sent and sent[0][1] == ""
