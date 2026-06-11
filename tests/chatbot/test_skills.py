"""Minimal tests for the humanizer skill (first concrete skill, implemented with maximal reuse).

Follows the same style and patterns as tests for memory.py and todo.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch, MagicMock

from plugin.chatbot.skills import SkillStore, HUMANIZER_GUIDANCE, HumanizerTool


class DummyCtx:
    """Matches the pattern used in test_memory.py so user_config_dir resolves cleanly."""
    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir

    def getServiceManager(self):
        sm = Mock()
        path_settings = Mock()
        path_settings.UserConfig = f"file://{self.tmp_dir}"
        sm.createInstanceWithContext.return_value = path_settings
        return sm


class _ToolContextLike:
    def __init__(self, inner_ctx):
        self.ctx = inner_ctx


class TestHumanizerSkillStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_default_guidance_when_no_file(self):
        store = SkillStore(self.ctx)
        guidance = store.get_humanizer_guidance()
        self.assertIn("HUMANIZER GUIDANCE", guidance)
        self.assertIn("Vary sentence length", guidance)
        # It should have seeded the file
        self.assertTrue(os.path.exists(store.get_humanizer_skill_path()))

    def test_user_override_wins(self):
        store = SkillStore(self.ctx)
        custom = "Custom rule: never use the word 'pivotal'."
        store.write_humanizer_guidance(custom)
        got = store.get_humanizer_guidance()
        self.assertEqual(got, custom)

    def test_front_matter_is_stripped(self):
        store = SkillStore(self.ctx)
        content = """---
name: humanizer
---
Vary your sentences.
"""
        store.write_humanizer_guidance(content)
        got = store.get_humanizer_guidance()
        self.assertIn("Vary your sentences", got)
        self.assertNotIn("name: humanizer", got)


class TestHumanizerTool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_disabled_returns_error(self):
        tool = HumanizerTool()
        orig = getattr(skills_mod := __import__("plugin.chatbot.skills", fromlist=["get_config_bool_safe"]), "get_config_bool_safe", None)
        try:
            skills_mod.get_config_bool_safe = lambda ctx, key: False
            with patch("plugin.chatbot.skills.user_config_dir", return_value=self.tmp_dir):
                res = tool.execute(self.ctx, text="Some AI slop text.")
                self.assertIn("disabled", res.get("error", "").lower())
        finally:
            if orig is not None:
                skills_mod.get_config_bool_safe = orig

    def test_calls_llm_with_guidance(self):
        tool = HumanizerTool()

        mock_client = MagicMock()
        mock_client.make_chat_request.return_value = {"content": "Much more human version."}

        with patch("plugin.framework.config.get_config_bool_safe", return_value=True), \
             patch("plugin.chatbot.skills.user_config_dir", return_value=self.tmp_dir), \
             patch("plugin.chatbot.skills.SkillStore") as mock_store_cls, \
             patch("plugin.framework.client.llm_client.LlmClient", return_value=mock_client):

            mock_store = MagicMock()
            mock_store.get_humanizer_guidance.return_value = "Be specific. Vary rhythm."
            mock_store_cls.return_value = mock_store

            res = tool.execute(self.ctx, text="The pivotal paradigm shift...")

            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["humanized"], "Much more human version.")

            # Verify the guidance was included in the prompt sent to the model
            call_args = mock_client.make_chat_request.call_args[1]
            prompt = call_args["messages"][0]["content"]
            self.assertIn("Be specific. Vary rhythm.", prompt)
            self.assertIn("The pivotal paradigm shift", prompt)


if __name__ == "__main__":
    unittest.main()
