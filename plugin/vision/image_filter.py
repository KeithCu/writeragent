# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Filter get_image tool based on chat text model multimodal capability."""

from __future__ import annotations

from typing import Any


def filter_get_image_for_text_only_model(tools: list[Any], vision: bool | None) -> list[Any]:
    """Drop get_image only when the configured chat text model is known not to have native vision.

    get_image only helps a model that can actually SEE the returned image. For the chat (openai)
    path the text model is known, so a text-only model shouldn't be offered it. The MCP path does NOT
    call this -- there we assume the connecting client is vision-capable and always expose it.
    ``vision`` is the caller's ``has_native_vision`` answer: unknown (None) or
    True keeps the tool. This file ships in LibrePy, so it does not import model_fetcher.
    """
    if vision is not False:
        return tools
    return [t for t in tools if getattr(t, "name", None) != "get_image"]
