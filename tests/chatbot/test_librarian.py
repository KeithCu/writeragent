# Tests for librarian.seed_user_profile_if_missing (issue #346: onboarding must never block work).

import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from plugin.chatbot.librarian import is_tour_request, seed_user_profile_if_missing
from plugin.chatbot.memory import MemoryStore


class DummyCtx:
    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir

    # Mocking getServiceManager so user_config_dir resolves here
    def getServiceManager(self):
        sm = Mock()
        path_settings = Mock()
        path_settings.UserConfig = f"file://{self.tmp_dir}"
        sm.createInstanceWithContext.return_value = path_settings
        return sm


class TestSeedUserProfile(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        self.store = MemoryStore(self.ctx)

    def test_seed_creates_profile_with_suggested_name(self):
        with patch("plugin.chatbot.librarian.get_suggested_user_name", return_value="Keith"):
            created = seed_user_profile_if_missing(self.ctx)

        self.assertTrue(created)
        raw = self.store.read("user")
        self.assertTrue(raw.strip(), "seeded profile must be non-empty so the onboarding gate passes")
        data = json.loads(raw)
        self.assertEqual(data.get("name"), "Keith")
        self.assertIn("auto-detected", data.get("name_source", ""))

    def test_seed_without_suggested_name_still_unblocks_gate(self):
        with patch("plugin.chatbot.librarian.get_suggested_user_name", return_value=None):
            created = seed_user_profile_if_missing(self.ctx)

        self.assertTrue(created)
        raw = self.store.read("user")
        self.assertTrue(raw.strip())
        data = json.loads(raw)
        self.assertNotIn("name", data)
        self.assertIn("name_source", data)

    def test_seed_never_overwrites_existing_profile(self):
        original = json.dumps({"name": "Existing"}, indent=2)
        self.store.write("user", original)

        with patch("plugin.chatbot.librarian.get_suggested_user_name", return_value="Keith"):
            created = seed_user_profile_if_missing(self.ctx)

        self.assertFalse(created)
        self.assertEqual(self.store.read("user"), original)

    def test_second_seed_is_a_no_op(self):
        with patch("plugin.chatbot.librarian.get_suggested_user_name", return_value="Keith"):
            self.assertTrue(seed_user_profile_if_missing(self.ctx))
            self.assertFalse(seed_user_profile_if_missing(self.ctx))

        seeded = json.loads(self.store.read("user"))
        self.assertEqual(seeded.get("name"), "Keith")



if __name__ == "__main__":
    unittest.main()


class TestTourRequestMatcher(unittest.TestCase):
    def test_clear_tour_requests_match(self):
        for text in (
            "give me a tour",
            "Show me around!",
            "Can I take the grand tour?",
            "what can WriterAgent do?",
            "teach me how to use WriterAgent",
            "introduce me to WriterAgent",
        ):
            self.assertTrue(is_tour_request(text), text)

    def test_normal_messages_do_not_match(self):
        for text in (
            "hello",
            "make a table",
            "tutorial please",  # 'tour' inside 'tutorial' must not match
            "yes, André is fine, and my favourite color is yellow",
            "",
        ):
            self.assertFalse(is_tour_request(text), text)
