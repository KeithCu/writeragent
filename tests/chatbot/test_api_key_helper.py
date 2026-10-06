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
