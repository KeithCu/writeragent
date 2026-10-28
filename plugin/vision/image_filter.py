# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Filter get_image tool based on chat text model multimodal capability."""

from __future__ import annotations

from typing import Any


def filter_get_image_for_text_only_model(tools: list[Any]) -> list[Any]:
    """Drop get_image only when the configured chat text model is known not to have native vision.

    get_image only helps a model that can actually SEE the returned image. For the chat (openai)
    path the text model is known, so a text-only model shouldn't be offered it. The MCP path does NOT
    call this -- there we assume the connecting client is vision-capable and always expose it.
    If the model's vision is unknown (None) or supported (True), keep the tool.
    On the UI thread, this gets the cached answer and kicks off a background lookup if cold.
    """
    from plugin.framework.client.model_fetcher import get_current_endpoint, get_text_model, has_native_vision

    if has_native_vision(get_text_model(), get_current_endpoint()) is not False:
        return tools
    return [t for t in tools if getattr(t, "name", None) != "get_image"]
