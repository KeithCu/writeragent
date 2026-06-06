# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

# WriterAgent — outbound HTTP / LLM client stack (see llm_client.py).
"""Shared HTTP client helpers and ``LlmClient``."""

from .errors import (
    # format_error_message is re-exported from the central implementation in
    # plugin.framework.errors (see 2026 error formatting centralization).
    format_error_for_display,
    format_error_message,
    is_audio_unsupported_error,
)

# Provider / endpoint detection (centralized in the 2026 janitor effort)
from .provider_detection import (
    get_provider_from_endpoint,
    is_local_host,
    is_openrouter_endpoint,
)
from .llm_client import (
    LlmClient,
    OPENROUTER_CHAT_EXTRA_BLOCKLIST,
    merge_openrouter_chat_extra,
    strip_leaked_chat_template_control_tokens,
)
from .analysis_client import run_analysis as run_trusted_analysis
from .embedding_client import EmbeddingBatch, embed_texts, get_embedding_model
from .embeddings_service import delete_paragraphs, index_paragraphs, knn_search
from .requests import sync_request
from .stream_normalizer import iterate_sse

__all__ = [
    "EmbeddingBatch",
    "run_trusted_analysis",
    "embed_texts",
    "get_embedding_model",
    "delete_paragraphs",
    "index_paragraphs",
    "knn_search",
    "LlmClient",
    "OPENROUTER_CHAT_EXTRA_BLOCKLIST",
    "format_error_for_display",
    "format_error_message",
    "get_provider_from_endpoint",
    "is_audio_unsupported_error",
    "is_local_host",
    "is_openrouter_endpoint",
    "iterate_sse",
    "merge_openrouter_chat_extra",
    "strip_leaked_chat_template_control_tokens",
    "sync_request",
]
