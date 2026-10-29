# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Client / LLM-wire specific error helpers.

Wire-specific formatting (HTTP response bodies, audio modality heuristics)
lives here. The cross-cutting i18n mapper is
:func:`plugin.framework.errors.format_error_message` — import it from there.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
import urllib.parse

from plugin.framework.i18n import _
from plugin.framework.errors import NetworkError, http_status_message, safe_json_loads
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
    body = err_body.strip() if err_body else ""
    mapped = http_status_message(status) if (status == 429 or not body) else None
    base = mapped if mapped is not None else _("HTTP Error {0} from AI Provider: {1}").format(status, reason)
    if not body:
        return base
    data = safe_json_loads(err_body)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or err.get("error") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            return base + ". " + str(detail)
    snippet = body.replace("\n", " ")[:400]
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


def _error_http_status(err: Any) -> int | None:
    if isinstance(err, dict):
        details = err.get("details")
    else:
        details = getattr(err, "details", None)
    status = details.get("status") if isinstance(details, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def is_400_input_validation(err: Any) -> bool:
    """Treat HTTP 400 with 'input validation' or 'bad request' as likely audio-format rejection (e.g. Together AI)."""
    # Bugfix: "400" in str(err) is true for any payload whose text happens
    # to contain those digits, including a non-400 body. The HTTP status is
    # already on details["status"] from handle_http_status via format_error_payload.
    if _error_http_status(err) != 400:
        return False
    if isinstance(err, dict):
        msg = str(err.get("message") or "").lower()
    else:
        msg = str(getattr(err, "message", None) or err).lower()
    return "input validation" in msg or "bad request" in msg


def is_image_unsupported_error(e: Any) -> bool:
    """True when the provider says the model takes no image input."""
    msg = str(e).lower()
    # OpenRouter 404: 'No endpoints found that support image input'
    if "support image input" in msg:
        return True
    return ("image" in msg or "vision" in msg) and ("not supported" in msg or "unsupported" in msg)


def stream_error_message(chunk: dict[str, Any]) -> str | None:
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
    stream_err = stream_error_message(parsed)
    if stream_err is not None:
        # The stream loop redacts an echoed API key. Sync chat, images, and
        # STT must do the same, or HTTP 200 raises the raw provider string.
        # The caller passes the key. Do not retry a finished body.
        stream_err = redact_secrets(stream_err, [api_key])
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
