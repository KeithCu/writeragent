# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

# WriterAgent — outbound HTTP / LLM client stack (see llm_client.py).
"""Shared HTTP client helpers and ``LlmClient``."""

from .errors import (
    format_error_for_display,
    format_error_message,
    is_audio_unsupported_error,
)
from .llm_client import (
    LlmClient,
    OPENROUTER_CHAT_EXTRA_BLOCKLIST,
    merge_openrouter_chat_extra,
    strip_leaked_chat_template_control_tokens,
)
from .requests import sync_request
from .stream_normalizer import iterate_sse

__all__ = [
    "LlmClient",
    "OPENROUTER_CHAT_EXTRA_BLOCKLIST",
    "format_error_for_display",
    "format_error_message",
    "is_audio_unsupported_error",
    "iterate_sse",
    "merge_openrouter_chat_extra",
    "strip_leaked_chat_template_control_tokens",
    "sync_request",
]
