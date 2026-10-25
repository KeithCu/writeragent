# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Minimal skills support, starting with the humanizer (inspired by Hermes-agent).

Re-uses the exact same user profile storage pattern as MemoryStore (user_config_dir + subdirectory).
The primary delivery mechanism is **ambient prompt injection** (see get_chat_system_prompt_for_document),
exactly like memory injection. This gives the model the rules "for free"
during any writing or document work without a separate tool call.

Users can turn the skill on/off via Settings and edit the rules by editing the SKILL.md file
(or via a future in-app editor). The file in the user profile always wins over the built-in default.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from plugin.framework.config import atomic_write_text, user_config_dir
from plugin.framework.errors import ConfigError

log = logging.getLogger(__name__)

# Compact, high-signal default for WriterAgent / office documents.
# Derived from the Hermes humanizer patterns (MIT) but trimmed and tuned for
# professional writing, apply_document_content workflows, and avoiding AI slop
# while preserving meaning and document intent.
HUMANIZER_GUIDANCE = """HUMANIZER GUIDANCE (make prose sound natural and human):

Core goal: Remove common AI tells and inject real voice, rhythm, specificity, and personality.
Apply this to any text you generate or revise for the document.

Key rules (prioritized for office/professional writing):
- Vary sentence length and structure naturally. Mix short punchy sentences with longer ones.
- Be specific and concrete. Replace vague claims ("vibrant", "crucial", "testament to") with real details.
- Use simple verbs: prefer "is", "has", "shows" over "serves as", "functions as", "underscores", "highlights its importance".
- Remove promotional / inflated language and "AI vocabulary": pivotal, enduring, landscape (abstract), fostering, showcasing, groundbreaking (when figurative), etc.
- Eliminate formulaic structures: rule-of-three lists, "not only X but Y", excessive em-dashes, bolded inline headers, emojis in body text.
- Drop chatbot artifacts: "Great question!", "I hope this helps", "Let me know if...", "In conclusion, the future looks bright".
- Avoid hedging and weasel words when you have evidence. Say what you mean directly.
- Add soul when appropriate: opinions, uncertainty ("I don't know how to feel about this"), first-person where it fits the voice, small tangents or personality.
- Preserve the user's intended meaning and any existing document style. Do not over-polish or change voice unless asked.

When revising via apply_document_content, run the output through these rules (silently) so the inserted text feels written by a thoughtful human, not generated.

If the user provides a personal writing sample or style preference, match that rhythm and word choice instead of the generic "natural" voice.
"""


def strip_skill_front_matter(content: str) -> str:
    """Strip leading YAML front matter delimited by exact '---' lines."""
    if not content:
        return ""
    text = content
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()

    # Very light front-matter strip (--- ... --- or just the body after first ---)
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[idx + 1:]).strip()
    return text.strip()


class SkillStore:
    """File-backed skill storage, modeled directly on MemoryStore for minimal new concepts.

    Currently supports only the humanizer skill (easy to generalize later).
    Lives under the same user profile directory as memories/ so users have one place
    to find and edit their agent "personality" files.
    """

    config_dir: str
    skills_dir: str

    def __init__(self, ctx: Any = None) -> None:
        del ctx
        config_dir = user_config_dir()
        if config_dir is None:
            raise ConfigError("User configuration directory is not available to resolve skill store path")
        self.config_dir = config_dir
        self.skills_dir = os.path.join(self.config_dir, "skills")

    def _humanizer_path(self) -> str:
        return os.path.join(self.skills_dir, "humanizer", "SKILL.md")

    def get_humanizer_guidance(self) -> str:
        """Return the current humanizer rules.

        User-edited file always wins. Falls back to the built-in default if missing or empty.
        Strips a simple YAML front-matter block if present (for Hermes compatibility).

        On first access we proactively write the default SKILL.md into the user's profile
        so they can immediately open and customize it.
        """
        path = self._humanizer_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            stripped = strip_skill_front_matter(content)
            # A front-matter-only file splits to an empty body.
            # That is the same as a missing file: use HUMANIZER_GUIDANCE
            # so the prompt still gets the humanizer rules.
            if stripped:
                return stripped
        except FileNotFoundError:
            # Seed the editable file from the built-in default the first time anyone asks for it.
            self.write_humanizer_guidance(HUMANIZER_GUIDANCE)
            return HUMANIZER_GUIDANCE
        except (OSError, UnicodeDecodeError) as e:
            log.warning("Failed to read user humanizer skill: %s", e)
            return HUMANIZER_GUIDANCE

        return HUMANIZER_GUIDANCE

    def write_humanizer_guidance(self, content: str) -> bool:
        """Persist a user-edited version of the humanizer rules."""
        try:
            path = self._humanizer_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            atomic_write_text(path, content.strip() + "\n", prefix=".skill-", fsync=False)
            return True
        except OSError as e:
            log.exception("Failed to write humanizer skill: %s", e)
            return False

    def get_humanizer_skill_path(self) -> str:
        """Return the full path to the editable SKILL.md for display in UI / docs."""
        return self._humanizer_path()


__all__ = ["SkillStore", "HUMANIZER_GUIDANCE", "strip_skill_front_matter"]
