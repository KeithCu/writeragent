# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared HTTP client helpers.

Heavy LLM symbols are lazy (PEP 562). LibrePy imports
``plugin.framework.client.requests`` for the weekly update check; loading this
package must not import ``llm_client`` (not in the core OXT).

Host embeddings, folder FTS, and langdetect RPC are not this HTTP client.
They live next to their callers (``plugin.embeddings``, ``plugin.writer.locale``).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .errors import format_error_for_display, is_audio_unsupported_error
from .provider_detection import get_provider_from_endpoint, is_local_host, is_openrouter_endpoint, is_openwebui_endpoint
from .requests import sync_request

if TYPE_CHECKING:
    from plugin.scripting.client import run_analysis as run_trusted_analysis

    from .llm_client import LlmClient, OPENROUTER_CHAT_EXTRA_BLOCKLIST, merge_openrouter_chat_extra, strip_leaked_chat_template_control_tokens
    from .stream_normalizer import iterate_sse

__all__ = [
    "run_trusted_analysis",
    "LlmClient",
    "OPENROUTER_CHAT_EXTRA_BLOCKLIST",
    "format_error_for_display",
    "get_provider_from_endpoint",
    "is_audio_unsupported_error",
    "is_local_host",
    "is_openrouter_endpoint",
    "is_openwebui_endpoint",
    "iterate_sse",
    "merge_openrouter_chat_extra",
    "strip_leaked_chat_template_control_tokens",
    "sync_request",
]

# name -> (module, attr). Relative names load against this package.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "LlmClient": (".llm_client", "LlmClient"),
    "OPENROUTER_CHAT_EXTRA_BLOCKLIST": (".llm_client", "OPENROUTER_CHAT_EXTRA_BLOCKLIST"),
    "merge_openrouter_chat_extra": (".llm_client", "merge_openrouter_chat_extra"),
    "strip_leaked_chat_template_control_tokens": (".response_normalizers", "strip_leaked_chat_template_control_tokens"),
    "iterate_sse": (".stream_normalizer", "iterate_sse"),
    "run_trusted_analysis": ("plugin.scripting.client", "run_analysis"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY_ATTRS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = spec
    value = getattr(importlib.import_module(module_name, __name__), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
