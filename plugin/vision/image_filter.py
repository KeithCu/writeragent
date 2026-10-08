# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Filter get_image tool based on chat text model multimodal capability."""

from __future__ import annotations

from typing import Any


def chat_text_model_has_native_vision() -> bool:
    """True when the configured chat text model can see images.

    Fail-open True when capability cannot be determined — same contract as
    filter_get_image_for_text_only_model (keep get_image rather than hide it).
    """
    try:
        from plugin.framework.client.model_fetcher import get_current_endpoint, get_text_model, has_native_vision

        return bool(has_native_vision(get_text_model(), get_current_endpoint(), allow_fetch=False, unknown_is_vision=True))
    except Exception:
        return True


def filter_get_image_for_text_only_model(tools: list[Any]) -> list[Any]:
    """Drop get_image when the configured CHAT text model has no native vision.

    get_image only helps a model that can actually SEE the returned image. For the chat (openai)
    path the text model is known, so a text-only model shouldn't be offered it. The MCP path does NOT
    call this -- there we assume the connecting client is vision-capable and always expose it.
    Fail OPEN: if the model's vision can't be determined, keep the tool rather than hide a working one.
    """
    if chat_text_model_has_native_vision():
        return tools
    return [t for t in tools if getattr(t, "name", None) != "get_image"]
