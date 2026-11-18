# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Minimal skills support, starting with the humanizer (inspired by Hermes-agent).

The primary delivery mechanism is **ambient prompt injection** (see get_chat_system_prompt_for_document),
exactly like memory injection. This gives the model the rules "for free"
during any writing or document work without a separate tool call.

Users can turn the skill on/off via Settings; the guidance always uses the built-in default.
"""

from __future__ import annotations

import logging

from plugin.framework import config as framework_config
from plugin.framework.prompts import fenced_data_block

log = logging.getLogger(__name__)

# Compact, high-signal default for WriterAgent / office documents.
# Derived from the Hermes humanizer patterns (MIT) but trimmed and tuned for
# professional writing, apply_content workflows, and avoiding AI slop
# while preserving meaning and document intent.
HUMANIZER_GUIDANCE = """HUMANIZER GUIDANCE (make prose sound natural and human):

Core goal: Remove common AI tells and give the prose natural rhythm and specific, concrete wording.
Apply this to text you write or rewrite for the document, not to chat replies or minimal corrections.

Key rules (prioritized for office/professional writing):
- Vary sentence length and structure naturally. Mix short punchy sentences with longer ones.
- Be specific and concrete. Replace vague claims ("vibrant", "crucial", "testament to") with real details.
- Use simple verbs: prefer "is", "has", "shows" over "serves as", "functions as", "underscores", "highlights its importance".
- Remove promotional / inflated language and "AI vocabulary": pivotal, enduring, landscape (abstract), fostering, showcasing, groundbreaking (when figurative), etc.
- Eliminate formulaic structures: reflexive three-item lists, "not only X but Y", excessive em-dashes, bolded inline headers, emojis in body text.
- Drop chatbot artifacts: "Great question!", "I hope this helps", "Let me know if...", "In conclusion, the future looks bright".
- Avoid hedging and weasel words when you have evidence. Say what you mean directly.
- Preserve the user's intended meaning and any existing document style. Do not over-polish or change voice unless asked.

When drafting or revising document text, run the output through these rules (silently) so the inserted text feels written by a thoughtful human, not generated.

If the user provides a personal writing sample or style preference, match that rhythm and word choice instead of the generic "natural" voice.
"""

# Scope rules sit on the heading rather than in HUMANIZER_GUIDANCE so the guidance body stays a
# generic style list; the heading decides where it applies (document prose, not chat replies).
HUMANIZER_PROMPT_HEADING = (
    "[HUMANIZER GUIDANCE — the user turned this on for prose you write or rewrite in the document (body, cell, slide, or shape text), not for chat replies. "
    "Keep the document's existing voice and formality: first person, opinions, tangents, and casual "
    "phrasing belong in chat replies only, even where the guidance below suggests them, unless the "
    "document already writes that way or the user asks. "
    "Minimal corrections (\"fix this\": spelling and grammar in the current sentence) are exempt.]"
)


def format_humanizer_guidance_prompt_block() -> str:
    """Return fenced humanizer guidance if enabled in settings, else empty string."""
    try:
        if framework_config.get_config("chatbot.humanizer_enabled"):
            return fenced_data_block(
                HUMANIZER_PROMPT_HEADING,
                HUMANIZER_GUIDANCE.strip(),
                tag="humanizer",
            )
    except Exception:
        log.exception("Failed to inject humanizer guidance")
    return ""
