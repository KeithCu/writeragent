"""Tests for the built-in humanizer skill guidance."""

import builtins
from unittest.mock import patch

from plugin.chatbot.skills import HUMANIZER_GUIDANCE, format_humanizer_guidance_prompt_block


def test_format_humanizer_guidance_uses_builtin_and_ignores_stale_skill_md(tmp_path):
    stale_skill = tmp_path / "skills" / "humanizer" / "SKILL.md"
    stale_skill.parent.mkdir(parents=True)
    stale_content = "When revising via apply_document_content, use first person and opinions."
    stale_skill.write_text(stale_content, encoding="utf-8")

    opened_paths: list[str] = []
    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):
        opened_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    with (
        patch("plugin.framework.config.get_config", side_effect=lambda key: key == "chatbot.humanizer_enabled"),
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("builtins.open", side_effect=tracking_open),
    ):
        block = format_humanizer_guidance_prompt_block()

    assert HUMANIZER_GUIDANCE.strip() in block
    assert stale_content not in block
    assert "apply_document_content" not in block
    assert not any(path.endswith("SKILL.md") for path in opened_paths)
    assert stale_skill.read_text(encoding="utf-8") == stale_content
