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

import json
import logging
import re
from typing import Any
import urllib.parse

from plugin.framework.i18n import _
from plugin.framework.errors import NetworkError, format_error_message, http_status_message
from plugin.framework.client.http_security import redact_secrets

log = logging.getLogger(__name__)

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
        mapped = http_status_message(status)
        if mapped is not None:
            return mapped
        return _("HTTP Error {0} from AI Provider: {1}").format(status, reason)
    base = http_status_message(status) if status == 429 else None
    if base is None:
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
    from plugin.framework.errors import WriterAgentException, resolve_exception_message

    # Drain loop ERROR items are format_error_payload dicts, not Exception.
    # format_error_payload(dict) would wrap the whole mapping as INTERNAL_ERROR.
    if isinstance(e, dict):
        msg = e.get("message") or e.get("code") or str(e)
        return _("Error: {0}").format(msg)
    if isinstance(e, WriterAgentException):
        message = e.message
    elif isinstance(e, BaseException):
        message = format_error_message(e)
    else:
        message = resolve_exception_message(e)
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


def is_image_unsupported_error(e: Any) -> bool:
    """True when the provider says the model takes no image input."""
    msg = str(e).lower()
    # OpenRouter 404: 'No endpoints found that support image input'
    if "support image input" in msg:
        return True
    return ("image" in msg or "vision" in msg) and ("not supported" in msg or "unsupported" in msg)


def _stream_error_message(chunk: dict[str, Any]) -> str | None:  # pyright: ignore[reportUnusedFunction]
    """Provider error carried inside an HTTP 200 SSE object, or None.

    ``error: null`` and an empty error object are not failures. A real
    ``{"type":"error"}`` or a non-empty top-level ``error`` object is.
    """
    err = chunk.get("error")
    if isinstance(err, dict) and err:
        message = err.get("message") or err.get("type") or "stream error"
        return str(message)
    if isinstance(err, str) and err.strip():
        return err.strip()
    event_type = chunk.get("type")
    if event_type == "error" or (isinstance(event_type, str) and event_type.endswith("_error")):
        return str(event_type)
    return None


# We match structured codes first and only narrow phrases here, not bare
# "too many" / "unavailable", because those also match fatal errors such as
# "too many tokens" (context overflow) or "model unavailable", which a retry
# cannot fix. The full HTTP reason phrases for 429 and 503 stay.
_OVERLOAD_TEXT_MARKERS = ("overload", "rate_limit", "rate limit", "too many requests", "service unavailable")

# Explicit HTTP status indicator (e.g. "HTTP 429", "status 503", "HTTP status 429", "error code 503").
# Bare 429 or 503 (such as "limit is 503 tokens" or "4290 tokens") must never match.
# Bugfix: previously _OVERLOAD_STATUS_RE matched any bare \b(?:429|503)\b, which caused non-overload
# errors like "limit is 503 tokens" to be wrongly identified as overload and retried until timeout.
# We fix this by checking structured error codes (including OpenRouter error.code) and explicit
# HTTP status indicators first, while the text fallback matches only phrases.
_HTTP_STATUS_OVERLOAD_RE = re.compile(
    r"\b(?:https?|status(?:\s+code)?|http\s+error|error\s+code)\s*(?:is\s*|:\s*)?(?:429|503)\b",
    re.IGNORECASE,
)


def _error_code_is_overload(value: Any) -> bool:
    """True when a structured provider code is itself an overload signal."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value in (429, 503, 529)
    if isinstance(value, float):
        return value in (429.0, 503.0, 529.0)
    text = str(value).strip().lower()
    if not text:
        return False
    if text in {"429", "503", "529"}:
        return True
    return any(marker in text for marker in _OVERLOAD_TEXT_MARKERS)


def _structured_error_is_overload(chunk: dict[str, Any]) -> bool:
    """Prefer error.code / type / status over scanning the message text."""
    if any(_error_code_is_overload(chunk.get(key)) for key in ("code", "status")):
        return True
    err = chunk.get("error")
    if isinstance(err, dict):
        if any(_error_code_is_overload(err.get(key)) for key in ("code", "type", "status")):
            return True
        metadata = err.get("metadata")
        if isinstance(metadata, dict) and any(_error_code_is_overload(metadata.get(key)) for key in ("code", "status", "http_status")):
            return True
    elif _error_code_is_overload(err):
        return True
    return False


def _text_is_overload(message: Any, *, status_code: int | None = None) -> bool:  # pyright: ignore[reportUnusedFunction]
    """Check if an error indicates an overload/rate-limit condition.

    Checks HTTP status / structured error code (including OpenRouter error.code
    in the body) first; the text fallback matches only phrases like 'overloaded' /
    'rate limit', no bare 429/503.
    """
    if isinstance(message, dict):
        return _structured_error_is_overload(message)
    if status_code is not None and _error_code_is_overload(status_code):
        return True
    if not message or not isinstance(message, str):
        return False

    # Check for embedded JSON with structured error code (e.g. OpenRouter error payload)
    if "{" in message and "}" in message:
        try:
            start = message.find("{")
            end = message.rfind("}")
            parsed = json.loads(message[start : end + 1])
            if isinstance(parsed, dict) and _structured_error_is_overload(parsed):
                return True
        except (ValueError, TypeError):
            pass

    # Check for explicit HTTP status indicator in text (e.g. "HTTP 429", "status 503")
    if _HTTP_STATUS_OVERLOAD_RE.search(message):
        return True

    # Text fallback: matches only phrases like 'overloaded'/'rate limit', no bare 429/503
    low = message.lower()
    return any(marker in low for marker in _OVERLOAD_TEXT_MARKERS)


def _stream_error_is_overload(chunk: dict[str, Any]) -> bool:  # pyright: ignore[reportUnusedFunction]
    """True when a mid-stream error is worth one retry before any UI text.

    A structured code (429, 503, rate_limit_exceeded, overloaded_error) wins.
    Otherwise explicit HTTP status or phrases match; bare numbers like 503 tokens do not.
    """
    if _structured_error_is_overload(chunk):
        return True
    message = _stream_error_message(chunk) or ""
    return _text_is_overload(message)


def _chat_request_payload_from_body(body: Any) -> dict[str, Any]:
    """Parse encoded chat JSON for diagnostics. Empty dict if unreadable."""
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _request_payload_byte_length(body: Any) -> int:
    """Byte length of the encoded request body for size diagnostics."""
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    return 0


def _redact_secret_from_log_text(text: str, secret: str) -> str:  # pyright: ignore[reportUnusedFunction]
    """Remove a configured credential from a log string. Empty secret is a no-op."""
    return redact_secrets(text, [secret] if secret else None)


def _parse_provider_envelope(raw: Any, path: str, *, api_key: str = "") -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
    """Parse an HTTP 200 provider body as a JSON object.

    ``safe_json_loads`` without ``strict=True`` repairs truncated model text,
    so a cut-off envelope such as
    ``{"choices":[{"message":{"content":"hel`` became a dict and looked like a
    finished reply. A JSON array was not ``None``, then ``.get`` ran outside
    the request ``try``. Provider envelopes are not model text.
    ``parse_strict_json`` is ``json.loads`` only, and only a dict is a response.
    """
    from plugin.framework.client.http_transport import parse_strict_json

    parsed = parse_strict_json(raw)
    if not isinstance(parsed, dict):
        raise NetworkError("LLM response was not JSON", code="BAD_RESPONSE", details={"url": path})
    # The stream loop raises on {"error": ...} inside HTTP 200. Sync chat,
    # images, and STT must do the same, or that body looks like a finished
    # reply and chat_completion_sync returns "". finish_reason=length with
    # empty content is a different, documented case.
    stream_err = _stream_error_message(parsed)
    if stream_err is not None:
        # The stream loop redacts an echoed API key. Sync chat, images, and
        # STT must do the same, or HTTP 200 raises the raw provider string.
        # The caller passes the key. Do not retry a finished body.
        stream_err = _redact_secret_from_log_text(stream_err, api_key)
        raise NetworkError(stream_err, code="STREAM_ERROR", details={"url": path})
    return parsed


def _full_url_for_request_path(endpoint: str, path: str) -> str:
    """Join stored endpoint host with relative API path for debug logs.

    Query string is dropped so a leftover ``?key=`` never appears in logs
    (Google image used to put the API key on the URL).
    """
    if not path or not str(path).startswith("/"):
        return path
    try:
        parsed = urllib.parse.urlparse(endpoint or "")
        if parsed.scheme and parsed.netloc:
            path_only = urllib.parse.urlparse(str(path)).path or str(path)
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path_only, "", "", ""))
    except ValueError:
        pass
    return path


def _path_without_query(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
    """Request path for logs. A leftover ``?key=`` must not be printed."""
    if not path or "?" not in path:
        return path
    return path.split("?", 1)[0]


def _log_chat_request_body_diag(client: Any, path: str, body: Any, headers: Any, tools: Any) -> None:  # pyright: ignore[reportUnusedFunction]
    """Log wire-level chat fields (no secrets) for provider debugging."""
    payload = _chat_request_payload_from_body(body)
    api_key = str(client.config.get("api_key") or "").strip()
    n_tools = len(tools) if isinstance(tools, list) else len(payload.get("tools") or [])
    log.debug("Chat Request body: model=%r stream=%s tools=%s full_url=%r api_key_set=%s api_key_len=%s", payload.get("model"), payload.get("stream"), n_tools, _full_url_for_request_path(client._endpoint(), path), bool(api_key), len(api_key))


def _prompt_char_count(messages: Any) -> int:
    """Sum of message text lengths for 500 diagnostics. Never logs the text."""
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
                elif isinstance(part, str):
                    total += len(part)
    return total


def _exit_code_from_provider_body(err_body: str) -> str | None:
    """Best-effort native exit code from an Ollama / llama-server 500 body."""
    if not err_body:
        return None
    matched = re.search(r"0x[0-9a-fA-F]+", err_body)
    if matched:
        return matched.group(0)
    matched = re.search(r"exit status ([^:\s]+)", err_body, flags=re.IGNORECASE)
    if matched:
        return matched.group(1)
    return None


def _log_http_500_request_diag(client: Any, response: Any, path: str, body: Any, err_body: str = "", n_ctx: int | None = None) -> None:  # pyright: ignore[reportUnusedFunction]
    """One ERROR-level safe request shape for HTTP 500. No prompts or secrets.

    Local llama-server / Ollama 500 bodies are often opaque. This is the
    request *shape* (counts, host/path, model) so a debug log can be compared
    with the reporter's server log without dumping the prompt or tool schemas.
    Extra fields (n_ctx, prompt_chars, exit_code, max_tokens) stay on this
    same line — do not thin the PR 571 fields when adding overflow context.
    The raw provider body stays on the separate ``Provider API Error 500`` line.
    """
    payload = _chat_request_payload_from_body(body)
    messages = payload.get("messages")
    tools = payload.get("tools")
    log.error(
        "HTTP 500 request diagnostic: provider=%s request_model=%r full_url=%r status=%s reason=%r stream=%s messages=%s tools=%s payload_bytes=%s n_ctx=%s prompt_chars=%s max_tokens=%s exit_code=%r",
        client._get_provider(),
        payload.get("model"),
        _full_url_for_request_path(client._endpoint(), path),
        response.status,
        getattr(response, "reason", ""),
        payload.get("stream"),
        len(messages) if isinstance(messages, list) else 0,
        len(tools) if isinstance(tools, list) else 0,
        _request_payload_byte_length(body),
        n_ctx,
        _prompt_char_count(messages),
        payload.get("max_tokens"),
        _exit_code_from_provider_body(err_body),
    )
