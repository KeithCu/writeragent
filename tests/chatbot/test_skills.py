"""Tests for the built-in humanizer skill guidance."""

from unittest.mock import patch

from plugin.chatbot.skills import HUMANIZER_GUIDANCE, SkillStore


class TestHumanizerSkillStore:
    def test_get_humanizer_guidance_returns_builtin_default(self):
        assert SkillStore().get_humanizer_guidance() == HUMANIZER_GUIDANCE

    def test_stale_skill_md_in_profile_is_ignored(self, tmp_path):
        stale_skill = tmp_path / "skills" / "humanizer" / "SKILL.md"
        stale_skill.parent.mkdir(parents=True)
        stale_content = "When revising via apply_document_content, use first person and opinions."
        stale_skill.write_text(stale_content, encoding="utf-8")

        with patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)):
            guidance = SkillStore().get_humanizer_guidance()

        assert guidance == HUMANIZER_GUIDANCE
        assert "apply_document_content" not in guidance
        assert stale_skill.read_text(encoding="utf-8") == stale_content
