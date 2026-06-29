"""Minimal tests for the humanizer skill (first concrete skill, implemented with maximal reuse).

Follows the same style and patterns as tests for memory.py and todo.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from plugin.chatbot.skills import SkillStore


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


def _skills_tree_root(skill_md_path: str) -> str:
    """Return the .../skills directory for a humanizer SKILL.md path."""
    return os.path.dirname(os.path.dirname(skill_md_path))


class _SkillTestBase(unittest.TestCase):
    """Isolate skill file I/O under a temp dir and remove seeded SKILL.md after each test."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        self._skill_paths_to_cleanup: list[str] = []
        self._user_config_patcher = patch(
            "plugin.chatbot.skills.user_config_dir",
            return_value=self.tmp_dir,
        )
        self._user_config_patcher.start()

    def tearDown(self):
        self._user_config_patcher.stop()
        for skill_md in self._skill_paths_to_cleanup:
            skills_root = _skills_tree_root(skill_md)
            if os.path.isdir(skills_root):
                shutil.rmtree(skills_root, ignore_errors=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _track_skill_path(self, store: SkillStore) -> None:
        self._skill_paths_to_cleanup.append(store.get_humanizer_skill_path())


class TestHumanizerSkillStore(_SkillTestBase):
    def test_default_guidance_when_no_file(self):
        store = SkillStore(self.ctx)
        guidance = store.get_humanizer_guidance()
        self._track_skill_path(store)
        self.assertIn("HUMANIZER GUIDANCE", guidance)
        self.assertIn("Vary sentence length", guidance)
        # It should have seeded the file
        self.assertTrue(os.path.exists(store.get_humanizer_skill_path()))

    def test_user_override_wins(self):
        store = SkillStore(self.ctx)
        custom = "Custom rule: never use the word 'pivotal'."
        store.write_humanizer_guidance(custom)
        self._track_skill_path(store)
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
        self._track_skill_path(store)
        got = store.get_humanizer_guidance()
        self.assertIn("Vary your sentences", got)
        self.assertNotIn("name: humanizer", got)


if __name__ == "__main__":
    unittest.main()
