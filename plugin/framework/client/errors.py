# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Client / LLM-wire specific error helpers.

Wire-specific formatting (HTTP response bodies, audio modality heuristics) and
``format_error_for_display`` live here. The cross-cutting i18n mapper is
:func:`plugin.framework.errors.format_error_message` — import it from there.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.i18n import _
from plugin.framework.errors import format_error_message

_ZAI_CODING_PLAN_ENDPOINT = "https://api.z.ai/api/coding/paas/v4"

# Issue #570: Ollama/llama.cpp died at prefill (Windows AV or prompt overflow).
_LLAMA_SERVER_CRASH_MARKERS = ("llama-server process has terminated", "0xc0000005", "truncating input prompt", "prompt overflow")


def format_context_window_label(num_ctx: Any) -> str | None:
    """Human window size for the crash sentence (4096 → 4K). None if unknown."""
    # float("inf") raises OverflowError ("cannot convert float infinity to
    # integer"), which (TypeError, ValueError) does not catch. Infinite or
    # overflowing values return None.
    try:
        window = int(float(num_ctx))
    except (TypeError, ValueError, OverflowError):
        return None
    if window <= 0:
        return None
    if window % 1024 == 0:
        return f"{window // 1024}K"
    return str(window)


def local_model_overflow_message(context_window: int | None = None) -> str:
    """Plain sidebar sentence: local llama-server died because the prompt overflowed."""
    label = format_context_window_label(context_window)
    if label:
        return _("The local Ollama/llama.cpp process crashed because the prompt overflowed a {0} context window.").format(label)
    return _("The local Ollama/llama.cpp process crashed because the prompt overflowed a too-small context window.")


def is_local_model_server_crash(text: Any) -> bool:
    """True for llama-server death / access violation / prompt-overflow 500 bodies."""
    blob = str(text or "")
    if not blob:
        return False
    lower = blob.lower()
    if any(marker in lower for marker in _LLAMA_SERVER_CRASH_MARKERS):
        return True
    if "llama.cpp" in lower and "overflow" in lower:
        return True
    if "overflowed a" in lower and "context window" in lower:
        return True
    return False


def _format_http_error_response(status: int, reason: str, err_body: str, context_window: int | None = None) -> str:  # pyright: ignore[reportUnusedFunction]  # shared by llm client tests and modality helpers
    """Build error message including response body for display in chat/UI.

    This remains client-specific because it parses provider error JSON bodies
    and falls back to raw snippets — behavior that is only relevant on the
    LLM HTTP path.

    Persistent ``http.client`` (``LlmClient``) never raises ``urllib``
    ``HTTPError``, so ``format_error_message()``'s 429 branch never runs on
    the chat drain. Map 429 here so Packet F2 shows a rate-limit sentence.

    HTTP 500 bodies that show llama-server died or the prompt overflowed
    become a plain sentence (issue #570). The raw dict stays in ERROR logs.
    Missing ``context_window`` is fine — the sentence still explains overflow.
    """
    if status == 500 and err_body and is_local_model_server_crash(err_body):
        return local_model_overflow_message(context_window)
    # Chat uses http.client, so format_error_message's urllib HTTPError
    # 401/403/404 sentences never run. An empty body uses those sentences;
    # a present body still appends the provider detail.
    if not err_body or not err_body.strip():
        if status == 401:
            return _("Invalid API Key. Please check your settings.")
        if status == 403:
            return _("API access Forbidden. Your key may lack permissions for this model.")
        if status == 404:
            return _("Endpoint not found (404). Check your URL and Model name.")
        if status == 429:
            return _("Rate limited (429). Wait a moment and try again.")
        return _("HTTP Error {0} from AI Provider: {1}").format(status, reason)
    if status == 429:
        base = _("Rate limited (429). Wait a moment and try again.")
    else:
        base = _("HTTP Error {0} from AI Provider: {1}").format(status, reason)
    from plugin.framework.errors import safe_json_loads

    data = safe_json_loads(err_body)
    if data is not None and isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or err.get("error") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            # Together and some providers return error.message as a dict, not a string.
            if not isinstance(detail, str):
                detail = str(detail)
            return base + ". " + detail
    snippet = err_body.strip().replace("\n", " ")[:400]
    return base + ".\nProvider Response:\n" + snippet


def append_zai_unknown_model_hint(message: str, err_body: Any, path: Any, provider: Any, request_model: Any = None) -> str:
    """Append Coding Plan endpoint guidance when Z.ai returns unknown-model 400."""
    if (provider or "").lower() != "zai":
        return message
    path_l = str(path or "").lower()
    if "/api/coding/" in path_l:
        return message
    err_l = str(err_body or "").lower()
    if "unknown model" not in err_l and '"code":"1211"' not in err_l and '"code": "1211"' not in err_l:
        return message
    hint = _(" If your API key is from a GLM Coding Plan subscription, set endpoint to {0} (not the general /api/paas URL).").format(_ZAI_CODING_PLAN_ENDPOINT)
    if request_model:
        return message + hint + _(" Request model was: {0}.").format(repr(request_model))
    return message + hint


def format_error_for_display(e: Any) -> str:
    """Return user-friendly error string for display in cells or dialogs."""
    from plugin.framework.errors import format_error_payload

    # Drain loop ERROR items are format_error_payload dicts, not Exception.
    # format_error_payload(dict) would wrap the whole mapping as INTERNAL_ERROR.
    if isinstance(e, dict):
        msg = e.get("message") or e.get("code") or str(e)
        return _("Error: {0}").format(msg)
    payload = format_error_payload(e)
    # dict.get evaluates its default before the lookup. format_error_message's
    # deal.pre requires an Exception, so a string raised PreContractError even
    # when the payload already had a message. Call the mapper only when the
    # key is absent.
    if "message" in payload:
        message = payload["message"]
    else:
        message = format_error_message(e)
    return _("Error: {0}").format(message)


def is_audio_unsupported_error(e: Any) -> bool:
    """Try to determine if the error indicates that audio/modality is unsupported by the model."""
    msg = str(e).lower()

    # Common error strings across providers
    if "unsupported content type" in msg:
        return True
    if "unsupported modality" in msg:
        return True
    if "audio" in msg and ("not supported" in msg or "unsupported" in msg):
        return True
    if "modality" in msg and "not supported" in msg:
        return True

    # Specific API error bodies (passed via _format_http_error_response)
    if "model" in msg and "cannot process" in msg and "audio" in msg:
        return True
    if "no endpoints found that support input audio" in msg:
        return True
    if "gpt-4" in msg and "audio" in msg:  # Some legacy GPT-4 might not have it
        if "not support" in msg:
            return True

    return False
