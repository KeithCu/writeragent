# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""LLM API client for WriterAgent.

Builds provider-aware LLM payloads and delegates chat HTTP execution to
``http_transport``. Transient 429/503 and connection errors get up to three total attempts
(OpenClaw packages/retry) with jittered abortable backoff (Retry-After
honoured) unless stream tokens already reached the UI. Request assembly still owns leaked chat-template token
stripping, dev/release system prefix, date prefix on first system message,
Anthropic/Gemini shims, OpenRouter merge (``merge_openrouter_chat_extra``), and
logging redaction. Takes a config dict from ``get_api_config`` and UNO ``ctx``.
Hosted providers with an empty API key raise ``AuthError`` from ``_resolve_auth``
(do not catch it into ``{}`` — that made the client look like ``custom`` and
surfaced a generic HTTP 401). Local/Ollama and ``header_style=none`` keep empty keys.

Concurrency: construct a **new** ``LlmClient`` for each job (sidebar send,
grammar worker, Calc ``=PROMPT()``, smol/web-research). The persistent HTTP
connection and provider shims (``_shims``) live on that instance only, so
chat and grammar hitting the same Ollama at once use two sockets, not one
shared conn. There is no process-wide client singleton. ``stop()`` (wired
from the sidebar Stop button via ``SendCancellation``) closes the socket
while the worker thread may still be reading the response — that is how a
hung stream is aborted, not a race to mutex away.
"""

from __future__ import annotations

import collections
import copy
import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import http.client

    from .base_provider_shim import BaseProviderShim

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20

from .response_normalizers import prepare_chat_messages, strip_leaked_chat_template_control_tokens


# Keys WriterAgent builds; openrouter_chat_extra must not replace these.
OPENROUTER_CHAT_EXTRA_BLOCKLIST: frozenset[str] = frozenset({"messages", "tools", "tool_choice", "stream"})


def merge_openrouter_chat_extra(base: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """Merge *extra* into *base* in place. Skips blocklisted keys; recurses into dict values."""
    if not extra:
        return
    for key, val in extra.items():
        if key in OPENROUTER_CHAT_EXTRA_BLOCKLIST:
            continue
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            merge_openrouter_chat_extra(base[key], val)
        elif isinstance(val, dict):
            base[key] = copy.deepcopy(val)
        else:
            base[key] = val


# accumulate_delta merges streaming deltas into message_snapshot; coalesce_split_tool_calls repairs OpenRouter empty-name split tool_calls.
from plugin.framework.async_stream import accumulate_delta, coalesce_split_tool_calls
from plugin.framework.constants import USER_AGENT

from plugin.framework.logging import init_logging, redact_sensitive_payload_for_log
from plugin.framework.client.auth import AuthError, resolve_auth_for_config, build_auth_headers, reject_control_chars_in_api_key
from plugin.framework.errors import NetworkError, WriterAgentException, is_disposed_exception
from plugin.framework.url_utils import get_api_version_suffix, normalize_endpoint_url

from plugin.framework.errors import format_error_message
from .errors import _format_http_error_response, append_zai_unknown_model_hint
from .http_transport import CONNECTION_ERRORS, LlmHttpTransport, parse_strict_json, redact_secrets
from .request_controls import RETRY_MAX_ATTEMPTS, backoff_delay_sec, clear_host_gap, emit_retry_status, pacing_key, remember_host_gap, request_model_from_body, wait_abortable
from .stream_normalizer import iterate_sse, _normalize_message_content, _normalize_delta, accumulate_streaming_thinking, extract_reasoning_replay_from_response, new_streaming_thinking_meta, THINKING_DELTA_KEYS
from .provider_detection import is_openrouter_endpoint

log = logging.getLogger(__name__)

# Anthropic Messages SSE has no OpenAI ``choices`` array. These event types
# still carry text, tool_use, or stop_reason.
_ANTHROPIC_STREAM_TYPES = frozenset({"content_block_start", "content_block_delta", "message_delta", "message_stop", "message"})


def _chunk_should_parse(chunk: dict[str, Any]) -> bool:
    """True when a stream JSON object should be handed to the provider shim."""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        return True
    return chunk.get("type") in _ANTHROPIC_STREAM_TYPES


def _stream_error_message(chunk: dict[str, Any]) -> str | None:
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
# 429/503 must be a whole token. "4290 tokens" is a context size, not HTTP 429.
_OVERLOAD_STATUS_RE = re.compile(r"\b(?:429|503)\b")


def _error_code_is_overload(value: Any) -> bool:
    """True when a structured provider code is itself an overload signal."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value in (429, 503)
    if isinstance(value, float):
        return value in (429.0, 503.0)
    text = str(value).strip().lower()
    if not text:
        return False
    if text in {"429", "503"}:
        return True
    return any(marker in text for marker in _OVERLOAD_TEXT_MARKERS)


def _structured_error_is_overload(chunk: dict[str, Any]) -> bool:
    """Prefer error.code / type / status over scanning the message text."""
    err = chunk.get("error")
    if not isinstance(err, dict):
        return False
    return any(_error_code_is_overload(err.get(key)) for key in ("code", "type", "status"))


def _text_is_overload(message: str) -> bool:
    low = message.lower()
    if any(marker in low for marker in _OVERLOAD_TEXT_MARKERS):
        return True
    return _OVERLOAD_STATUS_RE.search(low) is not None


def _stream_error_is_overload(chunk: dict[str, Any]) -> bool:
    """True when a mid-stream error is worth one retry before any UI text.

    A structured code (429, 503, rate_limit_exceeded, overloaded_error) wins.
    Otherwise 429 and 503 match only on a word boundary.
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


def _redact_secret_from_log_text(text: str, secret: str) -> str:
    """Remove a configured credential from a log string. Empty secret is a no-op."""
    return redact_secrets(text, [secret] if secret else None)


def _parse_provider_envelope(raw: Any, path: str, *, api_key: str = "") -> dict[str, Any]:
    """Parse an HTTP 200 provider body as a JSON object.

    ``safe_json_loads`` without ``strict=True`` repairs truncated model text,
    so a cut-off envelope such as
    ``{"choices":[{"message":{"content":"hel`` became a dict and looked like a
    finished reply. A JSON array was not ``None``, then ``.get`` ran outside
    the request ``try``. Provider envelopes are not model text.
    ``parse_strict_json`` is ``json.loads`` only, and only a dict is a response.
    """
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


def _path_without_query(path: str) -> str:
    """Request path for logs. A leftover ``?key=`` must not be printed."""
    if not path or "?" not in path:
        return path
    return path.split("?", 1)[0]


def _log_chat_request_body_diag(client: Any, path: str, body: Any, headers: Any, tools: Any) -> None:
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


# Do not store a miss in _ollama_show_cache: that cache is also the vision
# lookup, and a down server would then stick "no vision" for the process.


def _peek_live_ollama_num_ctx(client: Any) -> int | None:
    """Cached Ollama runtime num_ctx for crash copy / 500 logs. Never raises.

    ``query_ollama_runtime_num_ctx`` POSTs ``/api/show`` (10s) while
    ``llm_request_lane`` is still held on the HTTP 500 path. Format the 500
    from the body we already have. Read num_ctx only when a previous probe
    filled the show cache. Do not start a new HTTP call here.
    """
    try:
        if client._get_provider() != "ollama":
            return None
        model_name = str(client.config.get("model") or "").strip()
        if not model_name:
            return None
        endpoint = str(client._endpoint() or "")
        from plugin.framework.client.model_fetcher import _ollama_show_cache
        from plugin.framework.url_utils import normalize_endpoint_url

        cache_key = f"{normalize_endpoint_url(endpoint)}@{model_name}"
        cached = _ollama_show_cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        num_ctx = cached.get("num_ctx")
        if isinstance(num_ctx, int) and num_ctx > 0:
            return num_ctx
        return None
    except Exception:
        log.debug("HTTP 500: live num_ctx lookup failed", exc_info=True)
        return None


def _log_http_500_request_diag(client: Any, response: Any, path: str, body: Any, err_body: str = "", n_ctx: int | None = None) -> None:
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


from .openai_shim import get_provider_shim_class
from .stream_normalizer import ThinkTagStreamSplitter, strip_think_tags


class LlmClient:
    """LLM API client. Takes config dict from get_api_config() and UNO ctx."""

    config: dict[str, Any]
    ctx: Any
    _transport: LlmHttpTransport
    _stopped: bool

    def __init__(self, config: dict[str, Any], ctx: Any, cancellation_scope: Any | None = None, *, register_with_send: bool = True) -> None:
        self.config = config
        self.ctx = ctx
        self._transport = LlmHttpTransport(self._endpoint, self._timeout)
        self._shims: dict[str, BaseProviderShim] = {}
        # Stop before the first byte: close() alone is a no-op when sock is None;
        # the worker must not open a new connection (B13 / llm_request_lane).
        self._stopped = False
        scope = cancellation_scope
        if scope is None and register_with_send:
            try:
                from plugin.framework.queue_executor import get_current_send_cancellation

                scope = get_current_send_cancellation()
            except Exception:
                log.debug("LlmClient: could not resolve send cancellation scope", exc_info=True)
        if scope is not None and register_with_send:
            scope.register_client(self)

    def _get_shim(self) -> BaseProviderShim:
        """Get the provider shim for this client."""
        provider = self._get_provider()
        endpoint = self._endpoint()
        shim_key = f"{provider}:{endpoint}"
        if shim_key not in self._shims:
            shim_cls = get_provider_shim_class(provider)
            self._shims[shim_key] = shim_cls(self)
        return self._shims[shim_key]

    @property
    def _persistent_conn(self) -> http.client.HTTPConnection | http.client.HTTPSConnection | None:
        return self._transport.persistent_conn

    @property
    def _conn_key(self) -> tuple[str, str, int, str] | None:
        return self._transport.conn_key

    def _get_connection(self) -> http.client.HTTPConnection | http.client.HTTPSConnection:
        """Compatibility wrapper for tests and internal diagnostics."""
        if self._stopped:
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        return self._transport.get_connection()

    def _close_connection(self) -> None:
        self._transport.close()

    def _close_if_connection_close(self, response: Any) -> None:
        """Drop the keep-alive socket when the server sent ``Connection: close``.

        Call only after the body is fully read. Stop already closed the socket;
        a later ``read()`` would block until ``request_timeout`` and hold
        ``llm_request_lane``.
        """
        if self._stopped or not hasattr(response, "getheader"):
            return
        conn_hdr = (response.getheader("Connection") or "").strip().lower()
        if conn_hdr == "close":
            self._close_connection()

    def _observe_provider_http_error(self, response: Any, err_body: str, path: str, body: Any) -> str:
        """Chat-only ERROR line and llama-server 500 shape. Redaction is the transport's."""
        request_model = request_model_from_body(body)
        api_key = str(self.config.get("api_key") or "").strip()
        # Keep the existing response-body ERROR line, but never echo the key if
        # a provider (or proxy) reflected it in the error text.
        # A leftover ``?key=`` on the path must not land in the log. Strip the
        # query and redact any echoed secret before logging.
        safe_path = _redact_secret_from_log_text(_path_without_query(path), api_key)
        log.error("Provider API Error %d: %s (provider=%s path=%s request_model=%r)", response.status, _redact_secret_from_log_text(err_body, api_key), self._get_provider(), safe_path, request_model)
        n_ctx = _peek_live_ollama_num_ctx(self) if response.status == 500 else None
        if response.status == 500:
            _log_http_500_request_diag(self, response, path, body, err_body, n_ctx=n_ctx)
        err_msg = _format_http_error_response(response.status, response.reason, err_body, context_window=n_ctx)
        return append_zai_unknown_model_hint(err_msg, err_body, path, self._get_provider(), request_model)

    def _wire_secrets(self) -> list[str]:
        api_key = str(self.config.get("api_key") or "").strip()
        return [api_key] if api_key else []

    def _retry_or_raise_http_error(self, response: Any, body: Any, path: str, *, retries_left: int, emitted_any: bool, stop_checker: Any, status_callback: Any = None, attempt: int = 1) -> str | None:
        """On non-200: shared transport retry. Never after tokens already reached the UI."""
        action = self._transport.handle_http_status(
            response,
            request_body=body,
            path=path,
            retries_left=retries_left,
            emitted_any=emitted_any,
            stop_checker=stop_checker,
            status_callback=status_callback,
            attempt=attempt,
            secrets=self._wire_secrets(),
            observe_http_error=self._observe_provider_http_error,
        )
        if action == "stop":
            self._stopped = True
        return action

    def _send_http_attempt(self, method: str, path: str, body: Any, headers: dict[str, str], *, sends_left: int, wait_index: int, emitted_any: bool, stop_checker: Any, status_callback: Any) -> tuple[str, Any, int, int]:
        """One send. ``('ok', response, ...)`` on HTTP 200.

        ``('retry', None, ...)`` after a bounded 429/503 wait.
        ``('stop', None, ...)`` when Stop aborted that wait.
        Raises ``NetworkError`` on a terminal HTTP status.

        Connection errors stay with the caller: a mid-stream reset is caught
        around the body read, not only around this send.
        """
        response = self._send_request(method, path, body, headers, stop_checker=stop_checker, status_callback=status_callback)
        if response.status != 200:
            sends_left -= 1
            wait_index += 1
            action = self._retry_or_raise_http_error(response, body, path, retries_left=sends_left, emitted_any=emitted_any, stop_checker=stop_checker, status_callback=status_callback, attempt=wait_index)
            if action == "stop":
                return "stop", None, sends_left, wait_index
            return "retry", None, sends_left, wait_index
        if wait_index == 0:
            clear_host_gap(pacing_key(self._current_host(), request_model_from_body(body)))
        return "ok", response, sends_left, wait_index

    def _after_connection_error(self, err: Exception, *, path: str, body: Any, sends_left: int, wait_index: int, stop_checker: Any, status_callback: Any, retry_log_message: str) -> tuple[str, int, int]:
        """Shared connection-error budget. Returns ``('retry'|'stop', sends_left, wait_index)``."""
        sends_left -= 1
        wait_index += 1
        action = self._transport.handle_connection_error(err, path=path, retries_left=sends_left, retry_log_message=retry_log_message, stop_checker=stop_checker, status_callback=status_callback, attempt=wait_index, model=request_model_from_body(body), secrets=self._wire_secrets())
        return action, sends_left, wait_index

    def stop(self) -> None:
        """Abort the in-flight request: latch + close socket (even if not open yet).

        Packet B13: Stop can fire before ``get_connection``. Without ``_stopped``,
        the worker opens a fresh socket and holds ``llm_request_lane`` until timeout.
        """
        log.debug("LlmClient.stop() called")
        self._stopped = True
        self._close_connection()

    def clear_stop(self) -> None:
        """Allow a reused client to send again (call on the UI thread at send start)."""
        self._stopped = False

    def _abort_checker(self, stop_checker: Any) -> Any:
        """Checker for retry sleeps: ``stop()`` or the caller's predicate.

        ``wait_abortable`` returns early only when its checker is true.
        Streaming and sync tool requests passed the caller's checker alone, so
        ``stop()`` (which only sets ``_stopped``) slept out a backoff of up to
        ``RETRY_MAX_DELAY_SEC`` when the caller passed no checker.
        """

        def _aborted() -> bool:
            if self._stopped:
                return True
            return bool(stop_checker and stop_checker())

        return _aborted

    def _endpoint(self) -> str:
        raw = self.config.get("endpoint", "http://localhost:11434")
        return normalize_endpoint_url(raw, is_openwebui=self.config.get("is_openwebui", False))

    def _api_path(self) -> str:
        return get_api_version_suffix(self._endpoint(), is_openwebui=self.config.get("is_openwebui"))

    def _headers(self) -> dict[str, str]:
        """
        Build HTTP headers for API requests, including provider-aware auth.
        """
        h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        auth_info = self._resolve_auth()
        if auth_info:
            auth_headers = build_auth_headers(auth_info)
            h.update(auth_headers)

        # Legacy fallback for simple/manual endpoints: if an api_key exists and no
        # auth header was added (e.g. style='none' or unknown provider), add Bearer.
        # ``.get("api_key", "")`` still returns None when the key is present and
        # null, and str.strip on None raises AttributeError. Treat null like a
        # missing key and omit Bearer. Other call sites already use ``str(... or "")``.
        api_key = str(self.config.get("api_key") or "").strip()
        if api_key and "Authorization" not in h and "x-api-key" not in h:
            reject_control_chars_in_api_key(api_key)
            h["Authorization"] = f"Bearer {api_key}"

        return h

    def _resolve_auth(self) -> dict[str, Any]:
        """Resolve auth info from config.

        Swallowing ``AuthError`` here used to return ``{}``, so hosted missing
        keys looked like provider ``custom`` and the HTTP layer reported 401.
        Let ``missing_api_key`` / ``missing_endpoint`` / ``invalid_api_key``
        propagate. Ollama and custom empty keys never raise in
        ``resolve_auth_for_config``.
        """
        return resolve_auth_for_config(self.config)

    def _get_provider(self) -> str:
        """Get the provider ID from resolved auth."""
        auth_info = self._resolve_auth()
        return auth_info.get("provider", "custom")

    def _timeout(self) -> Any:
        """Settings read/stall budget. Connect uses ``LLM_CONNECT_TIMEOUT_SEC``."""
        return self.config.get("request_timeout", 120)

    def _current_host(self) -> str:
        return self._transport.current_host()

    def _enable_local_ssl_fallback(self, err: Exception) -> bool:
        """Compatibility wrapper for the transport-owned certificate fallback."""
        return self._transport.enable_local_ssl_fallback(err)

    def _send_request(self, method: str, path: str, body: Any, headers: dict[str, str], *, stop_checker: Any = None, status_callback: Any = None) -> Any:
        """Send through the transport while honoring tests/debuggers that override ``_get_connection`` on the instance."""
        if self._stopped:
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")

        def _stopped() -> bool:
            if self._stopped:
                return True
            return bool(stop_checker and stop_checker())

        connection_getter = self.__dict__.get("_get_connection")
        return self._transport.send(method, path, body, headers, connection_getter=connection_getter, stop_checker=_stopped, status_callback=status_callback)

    def make_api_request(self, prompt: str, system_prompt: str = "", max_tokens: int = 70) -> Any:
        """Build a streaming chat completions request (legacy/simple wrapper)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.make_chat_request(messages, max_tokens=max_tokens, stream=True)

    def extract_content_from_response(self, chunk: Any, stream_state: dict[str, Any] | None = None) -> Any:
        """Extract text content and optional thinking from response chunk (provider-aware)."""
        return self._get_shim().parse_response_chunk(chunk, stream_state)

    def make_chat_request(self, messages: list[Any], max_tokens: int = 512, tools: Any = None, stream: bool = False, model: str | None = None, response_format: Any = None, chat_extra: Any = None, *, prepend_dev_build_system_prefix: bool = True) -> tuple[str, str, Any, dict[str, str]]:
        """Build a chat completions request from a full messages array (provider-aware)."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 512

        messages = prepare_chat_messages(messages, self._get_provider(), prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        # One allow-list, including properties {}, runs here before any shim
        # builds the request. Otherwise each shim forwarded the model's tool
        # call unchanged, and hallucinated kwargs on a no-arg tool stayed in
        # the request until execute. OpenAI and Anthropic then format the
        # same checked call.
        from plugin.framework.tool_schema import normalize_outbound_tool_calls

        messages = normalize_outbound_tool_calls(messages, tools)

        model_name = model or self.config.get("model", "")
        # Missing key (settings default -1) means omit from the request so the
        # provider uses its own default. Explicit 0.0 from Settings still sends.
        temperature = self.config.get("temperature")

        shim = self._get_shim()
        method, path, body, headers = shim.build_chat_request(messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)

        init_logging(self.ctx)
        log.debug("=== Chat Request (provider=%s, tools=%s, stream=%s) ===" % (self._get_provider(), bool(tools), stream))
        log.debug("URL: %s" % _path_without_query(path))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Messages: %s" % json.dumps(redact_sensitive_payload_for_log(messages), indent=2))
        _log_chat_request_body_diag(self, path, body, headers, tools)

        return method, path, body, headers

    def make_image_request(self, prompt: str, model: str | None = None, width: int = 1024, height: int = 1024, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> Any:
        """Build an image generation request (provider-aware)."""
        shim = self._get_shim()
        return shim.build_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)

    def _exchange_json(self, method: str, path: str, body: Any, headers: dict[str, str], *, stop_checker: Any = None, status_callback: Any = None, retry_log_message: str, on_retry: Any = None, wrap_unexpected: bool = False, failure_log: str = "JSON request failed") -> tuple[str, Any]:
        """One non-streaming JSON exchange with the shared retry budget.

        Returns ``("ok", parsed)`` or ``("stop", None)``. The stream loop stays
        separate: it tracks ``emitted_any`` and parses SSE.

        ``_request_json`` and the sync half of ``request_with_tools`` each
        owned this loop. Image and speech only watched ``self._stopped``, so
        a caller's stop checker slept out a 429 and never got a retry status.
        A connection error after ``read()`` had already returned the body
        re-posted the request. One loop: Stop is a result the caller maps
        (raise vs stop dict). Bytes already in hand are not sent again.
        """
        abort_checker = self._abort_checker(stop_checker)
        if self._stopped or (stop_checker and stop_checker()):
            self._stopped = True
            self._close_connection()
            return "stop", None
        api_key = str(self.config.get("api_key") or "").strip()

        def _sender(send_method: str, send_path: str, send_body: Any, send_headers: dict[str, str], *, stop_checker: Any = None, status_callback: Any = None) -> Any:
            return self._send_request(send_method, send_path, send_body, send_headers, stop_checker=stop_checker, status_callback=status_callback)

        try:
            # Same exchange as catalog and speech. Stop, timeout, retry, and
            # redaction are not a second loop here.
            result = self._transport.exchange(
                method,
                path,
                body,
                headers,
                stop_checker=abort_checker,
                status_callback=status_callback,
                parse_json=False,
                sender=_sender,
                secrets=self._wire_secrets(),
                on_retry=on_retry,
                after_read=self._close_if_connection_close,
                observe_http_error=self._observe_provider_http_error,
                retry_log_message=retry_log_message,
            )
        except NetworkError as exc:
            if getattr(exc, "code", None) == "STOPPED":
                self._stopped = True
                return "stop", None
            raise
        except Exception as exc:
            if not wrap_unexpected:
                raise
            err_msg = format_error_message(exc)
            log.exception(failure_log)
            raise NetworkError(err_msg, details={"url": path}) from exc
        return "ok", _parse_provider_envelope(result.body, path, api_key=api_key)

    def _request_json(self, method: str, path: str, body: Any, headers: dict[str, str], *, stop_checker: Any = None, status_callback: Any = None) -> Any:
        """Blocking JSON call on the persistent transport.

        Image and speech used ``sync_request``, which ignored Stop and 429/503
        backoff. This shares the chat transport so ``stop()`` closes the socket.
        ``stop_checker`` / ``status_callback`` match the sync chat path: a 429
        wait ends when the caller stops, and the sidebar sees the retry line.
        """
        action, parsed = self._exchange_json(method, path, body, headers, stop_checker=stop_checker, status_callback=status_callback, retry_log_message="Retrying JSON request on fresh connection")
        if action == "stop":
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        return parsed

    def image_completion(self, prompt: str, model: str | None = None, width: int = 1024, height: int = 1024, steps: int | None = None, source_image: str | None = None, image_url: str | None = None, *, stop_checker: Any = None, status_callback: Any = None) -> Any:
        """Generate images using the configured provider. Returns list of base64 strings."""
        method, path, body, headers = self.make_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)
        endpoint = self._endpoint()
        if path.startswith("/"):
            parsed = urllib.parse.urlparse(endpoint)
            url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        else:
            url = path

        # log.debug...
        init_logging(self.ctx)
        log.debug("=== Image Request ===")
        # Path/query must not include API keys (Google image used to put ?key= here).
        log.debug("URL: %s" % urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(query="", fragment="")))

        # Image generate/edit used sync_request, which Stop cannot abort and
        # which does not retry 429/503. The persistent transport does both.
        try:
            res = self._request_json(method, path, body, headers, stop_checker=stop_checker, status_callback=status_callback)
        except NetworkError as e:
            if "not supported" in str(e):
                from plugin.framework.client.base_provider_shim import adjust_image_body_for_rejection
                new_body = adjust_image_body_for_rejection(body, str(e))
                if new_body and (stop_checker is None or not stop_checker()):
                    # The adjusted body is multi-MB base64. Log a 500-character
                    # preview plus the total length, not the whole payload.
                    body_text = new_body.decode("utf-8", errors="replace")
                    body_preview = f"{body_text[:500]}... [total {len(body_text)} chars]" if len(body_text) > 500 else body_text
                    log.warning("Image API rejected params, retrying with adjusted body: %s", body_preview)
                    res = self._request_json(method, path, new_body, headers, stop_checker=stop_checker, status_callback=status_callback)
                else:
                    raise
            else:
                raise

        if not res:
            return []

        shim = self._get_shim()
        return shim.parse_image_responses(res)

    def transcribe_audio(self, wav_path: str, model: str | None = None, *, stop_checker: Any = None, status_callback: Any = None) -> str:
        """Transcribe audio via POST /v1/audio/transcriptions (or chat if STT model supports input_audio).

        STT-only models use the transcription endpoint only; chat+audio STT models may
        try chat completions first. See docs/chat/audio-architecture.md.
        """
        import uuid
        import os
        import base64
        from plugin.framework.client.model_fetcher import has_native_audio
        from plugin.framework.url_utils import get_url_path_and_query

        # Determine model
        # Client dict may carry the Speech-tab key or the legacy top-level key.
        model_name = model or self.config.get("audio.stt_model") or self.config.get("stt_model") or "whisper-1"

        # 1. Check if the STT model itself supports native audio.
        # None means unknown. has_native_audio documents that as "try native";
        # a bare `if` treated None as false and skipped chat for uncatalogued models.
        if has_native_audio(model_name, self._endpoint()) is not False:
            log.warning("Using multimodal chat for transcription fallback (model: %s)", model_name)
            try:
                with open(wav_path, "rb") as f:
                    audio_b64 = base64.b64encode(f.read()).decode("utf-8")

                messages = [{"role": "user", "content": [{"type": "text", "text": "Transcribe this audio exactly. Output ONLY the transcript. No preamble, no markers."}, {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}]}]

                # Using synchronous chat completion with model override.
                # Pass status_callback so 429 waits are visible on this path too.
                return self.chat_completion_sync(
                    messages,
                    max_tokens=16384,
                    model=model_name,
                    stop_checker=stop_checker,
                    status_callback=status_callback,
                )
            except AuthError:
                raise
            except NetworkError as e:
                if self._stopped or getattr(e, "code", None) == "STOPPED":
                    raise
                # Only a transport failure is a reason to try
                # /audio/transcriptions. chat_completion_sync raises ValueError,
                # JSONDecodeError, or other non-network errors before an HTTP
                # failure; treating those as "this model cannot take
                # input_audio" would POST the transcription endpoint for a
                # bug. AuthError is re-raised above. Stop and USER_STOPPED
                # are not NetworkError, so they propagate. Any other exception
                # propagates too.
                log.exception("Multimodal transcription failed; falling back to stt endpoint")

        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/audio/transcriptions"
        headers = self._headers()

        # OpenRouter STT uses JSON + base64 input_audio, not OpenAI-style multipart/form-data.
        if is_openrouter_endpoint(endpoint, explicit_is_openrouter=self.config.get("is_openrouter")):
            with open(wav_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            body_bytes = json.dumps({"model": model_name, "input_audio": {"data": audio_b64, "format": "wav"}}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            # Standard multipart fallback (OpenAI Whisper, local servers, etc.)
            boundary = "Boundary-%s" % uuid.uuid4().hex
            parts = []
            filename = os.path.basename(wav_path)
            parts.append(("--%s" % boundary).encode("utf-8"))
            parts.append(('Content-Disposition: form-data; name="file"; filename="%s"' % filename).encode("utf-8"))
            parts.append(b"Content-Type: audio/wav")
            parts.append(b"")
            with open(wav_path, "rb") as f:
                parts.append(f.read())
            parts.append(("--%s" % boundary).encode("utf-8"))
            parts.append(('Content-Disposition: form-data; name="model"').encode("utf-8"))
            parts.append(b"")
            parts.append(model_name.encode("utf-8"))
            parts.append(("--%s--" % boundary).encode("utf-8"))
            parts.append(b"")
            headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
            body_bytes = b"\r\n".join(parts)

        log.debug("=== STT Request ===")
        log.debug("URL: %s" % url)
        log.debug("STT Model: %s" % model_name)

        # Same transport as chat so Stop closes the socket and 429/503 retries.
        # Passing api_path alone drops the endpoint's own path prefix.
        # get_url_path_and_query keeps both.
        req_path = get_url_path_and_query(url)
        res = self._request_json("POST", req_path, body_bytes, headers, stop_checker=stop_checker, status_callback=status_callback)
        return res.get("text", "") if isinstance(res, dict) else str(res)

    def stream_completion(self, prompt: str, system_prompt: str, max_tokens: int, append_callback: Any, append_thinking_callback: Any = None, stop_checker: Any = None, status_callback: Any = None) -> None:
        """Stream a chat completions response via callbacks."""
        method, path, body, headers = self.make_api_request(prompt, system_prompt, max_tokens)
        self.stream_request(method, path, body, headers, append_callback, append_thinking_callback, stop_checker=stop_checker, status_callback=status_callback)

    def _run_streaming_loop(self, method: str, path: str, body: Any, headers: dict[str, str], on_content: Any, on_thinking: Any = None, on_delta: Any = None, stop_checker: Any = None, _retry: bool = True, status_callback: Any = None, reset_unemitted_attempt: Any = None) -> Any:
        """Common low-level streaming engine."""
        init_logging(self.ctx)
        log.info("=== Starting streaming loop (persistent) ===")
        log.debug("Request Path: %s" % path)

        # Do not clear ``_stopped`` here — that races with stop() on another thread
        # (B13). UI clears via ``clear_stop()`` at the start of a new send.
        if self._stopped or (stop_checker and stop_checker()):
            log.debug("streaming_loop: Stop already requested before connect")
            self._stopped = True
            self._close_connection()
            return "stop"

        abort_checker = self._abort_checker(stop_checker)
        sends_left = RETRY_MAX_ATTEMPTS if _retry else 1
        wait_index = 0
        emitted_any = False
        while True:
            # on_delta writes role, usage, and a buffered "<think" prefix into
            # the caller's snapshot before any callback runs. emitted_any stays
            # false for a usage-only chunk and a partial tag, so an overload or
            # connection retry would call accumulate_delta again and add
            # prompt_tokens / completion_tokens and concatenate that prefix.
            # Drop only that attempt, and only while nothing has been shown.
            # Once emitted_any is set this does not run, so shown text stays.
            # The first call clears an empty snapshot; a later call drops the
            # failed attempt before the next send.
            if not emitted_any and reset_unemitted_attempt is not None:
                reset_unemitted_attempt()
            last_finish_reason = None

            try:
                if self._stopped or (stop_checker and stop_checker()):
                    log.debug("streaming_loop: Stop before send")
                    self._stopped = True
                    self._close_connection()
                    return "stop"
                action, response, sends_left, wait_index = self._send_http_attempt(method, path, body, headers, sends_left=sends_left, wait_index=wait_index, emitted_any=emitted_any, stop_checker=abort_checker, status_callback=status_callback)
                if action == "stop":
                    return "stop"
                if action == "retry":
                    continue

                # Set only when the SSE loop finishes without an error or Stop.
                # The finally block drains solely in that case.
                clean_finish = False
                try:
                    # Use a flag to stop logical processing but keep reading to exhaust the stream
                    content_finished = False
                    # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                    last_contents: collections.deque[str] = collections.deque(maxlen=REPEATED_STREAMING_CHUNK_LIMIT)
                    think_tag_splitter = ThinkTagStreamSplitter()
                    requested_model = request_model_from_body(body)
                    used_model = None
                    retry_outer = False
                    stream_state: dict[str, Any] = {}

                    for payload in iterate_sse(response):
                        if payload == "[DONE]":
                            log.info("streaming_loop: [DONE] received")
                            content_finished = True
                            continue

                        try:
                            # Same strict parser as sync chat and catalog. A
                            # truncated SSE line is skipped, not repaired into
                            # a finished chunk.
                            chunk = parse_strict_json(payload)
                        except NetworkError as decode_err:
                            if getattr(decode_err, "code", None) != "BAD_RESPONSE":
                                raise
                            if payload and payload != "{}":
                                log.exception("streaming_loop: JSON decode error in payload: %s", payload)
                            continue

                        # Valid JSON that is not an object (array / string / number) is not a
                        # chat chunk. .get would raise and abort the stream; skip so later
                        # well-formed chunks can still apply.
                        if type(chunk) is not dict:
                            continue

                        chunk_model = chunk.get("model")
                        if chunk_model and used_model is None:
                            used_model = str(chunk_model)
                            log.info("LLM response stream started: provider=%s requested_model=%r used_model=%r", self._get_provider(), requested_model, used_model)

                        usage_obj = chunk.get("usage")
                        if not isinstance(usage_obj, dict) or not usage_obj:
                            usage_obj = None
                        elif "usage" in chunk:
                            log.debug("streaming_loop: received usage: %s" % chunk["usage"])

                        if content_finished:
                            continue

                        # stop() sets _stopped and closes the socket, but http.client
                        # can already have buffered SSE lines. Checking only the
                        # caller's stop_checker still parsed those and passed
                        # them to on_content when the caller passed no checker.
                        # abort_checker includes the latch, same as the pre-send
                        # and retry-sleep paths.
                        if abort_checker():
                            log.debug("streaming_loop: Stop requested.")
                            last_finish_reason = "stop"
                            content_finished = True
                            self._stopped = True
                            # Kill the socket; do not keep reading (continue used to
                            # fall into finally:response.read() and block ~request_timeout —
                            # B13 held llm_request_lane for 60s after Stop).
                            self._close_connection()
                            break

                        # A 200 stream can still carry a provider error object. Skipping it
                        # used to look like a short successful answer.
                        stream_err = _stream_error_message(chunk)
                        if stream_err is not None:
                            # Chat HTTP errors already redact an echoed key. A 200
                            # SSE error used to raise the raw provider string, which
                            # the sidebar and the debug log then showed.
                            api_key = str(self.config.get("api_key") or "").strip()
                            stream_err = _redact_secret_from_log_text(stream_err, api_key)
                            if (not emitted_any) and _stream_error_is_overload(chunk) and sends_left > 1:
                                self._close_connection()
                                sends_left -= 1
                                wait_index += 1
                                delay = backoff_delay_sec(attempt=wait_index)
                                remember_host_gap(pacing_key(self._current_host(), requested_model), delay)
                                log.warning("Retrying mid-stream error after %.3fs (%s)", delay, stream_err)
                                emit_retry_status(status_callback, delay)
                                if not wait_abortable(delay, abort_checker):
                                    self._stopped = True
                                    return "stop"
                                retry_outer = True
                                break
                            raise NetworkError(stream_err, code="STREAM_ERROR", details={"url": path})

                        # Grok/xAI sends a final chunk with empty choices + usage.
                        # Anthropic SSE events have no choices array; dropping them
                        # discarded text_delta and tool_use before the shim could parse.
                        # Usage-only chunks still reach on_delta so request_with_tools
                        # can fill its usage field. They are not emitted text.
                        if not _chunk_should_parse(chunk):
                            if usage_obj and on_delta:
                                on_delta({"usage": usage_obj})
                            continue

                        content, finish_reason, thinking, delta = self.extract_content_from_response(chunk, stream_state)

                        # Keep the provider's parsed SSE shape before normalization. Some
                        # OpenAI-compatible routes have appeared to continue one function's
                        # arguments under a new tool-call index; the completed snapshot alone
                        # cannot tell whether that index came from the wire or our accumulator.
                        raw_tool_calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                        if raw_tool_calls is not None:
                            log.debug("streaming_loop: raw tool_call delta route_provider=%s chunk_provider=%r chunk_model=%r chunk_id=%r tool_calls=%s", self._get_provider(), chunk.get("provider"), chunk.get("model"), chunk.get("id"), json.dumps(raw_tool_calls, ensure_ascii=False))

                        # LiteLLM: streaming_handler.py ~L736 "finish_reason: error, no content string given"
                        if finish_reason == "error":
                            from plugin.framework.i18n import _

                            raise NetworkError(_("Stream ended with finish_reason=error"), code="STREAM_ERROR")

                        if thinking:
                            # A reasoning delta still reaches on_delta even when
                            # on_thinking is missing. Count those bytes, or a
                            # socket drop retries text the UI already has.
                            emitted_any = True
                            if on_thinking:
                                on_thinking(thinking)
                        if content:
                            pieces = think_tag_splitter.feed(content)
                            for is_think, text_piece in pieces:
                                if is_think:
                                    # <think> text must set emitted_any. A drop during
                                    # the reasoning block would otherwise retry
                                    # and duplicate it.
                                    if text_piece:
                                        emitted_any = True
                                        if on_thinking:
                                            on_thinking(text_piece)
                                else:
                                    if text_piece:
                                        emitted_any = True
                                    if on_content:
                                        on_content(text_piece)
                                    # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                                    last_contents.append(text_piece)
                                    if len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT and len(text_piece) > 2 and all(c == last_contents[0] for c in last_contents):
                                        from plugin.framework.i18n import _

                                        raise NetworkError(_("The model is repeating the same chunk (infinite loop). Try again or use a different model."), code="INFINITE_LOOP")
                        if raw_tool_calls:
                            # Tool-call deltas still call on_delta, and
                            # accumulate_delta concatenates function.name and
                            # arguments onto a snapshot created outside the
                            # retry loop. A socket drop after those chunks would
                            # merge a second copy of the call. Count tool-call
                            # bytes like emitted text so the retry branch raises
                            # CONNECTION_LOST. Do not clear or replay the
                            # snapshot; callbacks have already run.
                            emitted_any = True
                        outgoing: dict[str, Any] | None = delta if isinstance(delta, dict) else None
                        if usage_obj:
                            outgoing = dict(outgoing or {})
                            outgoing["usage"] = usage_obj
                        if outgoing and on_delta:
                            _normalize_delta(outgoing)
                            if chunk_model and "model" not in outgoing:
                                outgoing["model"] = str(chunk_model)
                            on_delta(outgoing)

                        if finish_reason:
                            log.debug("streaming_loop: logical finish_reason=%s" % finish_reason)
                            last_finish_reason = finish_reason

                    if retry_outer:
                        continue

                    if not content_finished and not last_finish_reason:
                        # iterate_sse finishes quietly on EOF, so a socket that
                        # closed early looked like a successful answer with
                        # finish_reason None. A missing [DONE] / finish_reason
                        # is a drop. Raise a real connection error so the outer
                        # loop can retry (if nothing was emitted) or surface
                        # CONNECTION_LOST.
                        raise ConnectionResetError("Stream truncated without [DONE] or finish_reason")

                    log.info("LLM response stream finished: provider=%s requested_model=%r used_model=%r finish_reason=%s", self._get_provider(), requested_model, used_model or requested_model, last_finish_reason)

                    # Flush any trailing buffered text from the think tag splitter
                    # (trailing buffer contains small tag prefix remnants like '<' at EOF).
                    # The splitter holds at most a tag prefix. Flushing after Stop
                    # would still call on_content for a partial "<think". A
                    # completed stream still needs the flush; Stop must not
                    # paint that prefix into the sidebar.
                    if not self._stopped:
                        for is_think, text_piece in think_tag_splitter.flush():
                            if is_think and text_piece:
                                emitted_any = True
                                if on_thinking:
                                    on_thinking(text_piece)
                            elif not is_think and on_content:
                                if text_piece:
                                    emitted_any = True
                                on_content(text_piece)
                    clean_finish = not self._stopped
                finally:
                    # STREAM_ERROR, INFINITE_LOOP, finish_reason error, and any
                    # other raised stream exception must not fall into
                    # response.read(). That waits out the rest of the generation
                    # (up to request_timeout, holding llm_request_lane). Drain
                    # only after a clean finish so the keep-alive socket can be
                    # reused. Every other exit closes. Stop still skips the
                    # drain (B13).
                    if clean_finish:
                        try:
                            remaining = response.read()
                            if remaining:
                                log.debug("Consumed extra %d bytes after loop" % len(remaining))
                        except Exception:
                            pass
                        # Honor Connection: close so we don't try to reuse when the server closed.
                        self._close_if_connection_close(response)
                    else:
                        try:
                            self._close_connection()
                        except Exception:
                            pass

            except CONNECTION_ERRORS as e:
                # stop() closes the socket, so the blocked read raises here.
                # Looking only at emitted_any reported Stop after the first
                # token as CONNECTION_LOST. A latched Stop (or the abort
                # checker) is a user cancel, same as the pre-token path. A
                # real drop still refuses retry once any token reached the UI.
                if self._stopped or abort_checker():
                    self._stopped = True
                    self._close_connection()
                    return "stop"
                # A retry after tokens already reached the UI would duplicate text.
                if emitted_any:
                    self._close_connection()
                    raise NetworkError(format_error_message(e), code="CONNECTION_LOST", details={"url": path}) from e
                action, sends_left, wait_index = self._after_connection_error(e, path=path, body=body, sends_left=sends_left, wait_index=wait_index, stop_checker=abort_checker, status_callback=status_callback, retry_log_message="Retrying streaming request on fresh connection")
                if action == "stop":
                    # Same latch as the pre-send and in-loop Stop paths. Without it
                    # the shared post-processing saw finish_reason "stop" plus any
                    # tool_calls already accumulated and rewrote it to "tool_calls".
                    self._stopped = True
                    return "stop"
                continue
            except NetworkError as e:
                if getattr(e, "code", None) == "STOPPED":
                    self._stopped = True
                    return "stop"
                raise
            except Exception as e:
                if isinstance(e, WriterAgentException) or is_disposed_exception(e):
                    raise
                err_msg = format_error_message(e)
                log.exception("streaming_loop: Unexpected error")
                raise NetworkError(err_msg, details={"url": path}) from e

            # If we completed successfully without retry, return
            return last_finish_reason

    def stream_request(self, method: str, path: str, body: Any, headers: dict[str, str], append_callback: Any, append_thinking_callback: Any = None, stop_checker: Any = None, status_callback: Any = None) -> None:
        """Streaming request for chat completions, using persistent connection."""
        init_logging(self.ctx)
        self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, stop_checker=stop_checker, status_callback=status_callback)

    def stream_chat_response(self, messages: list[Any], max_tokens: int, append_callback: Any, append_thinking_callback: Any = None, stop_checker: Any = None, status_callback: Any = None, *, prepend_dev_build_system_prefix: bool = True) -> None:
        """Stream a final chat response (no tools) using the messages array."""
        method, path, body, headers = self.make_chat_request(messages, max_tokens, tools=None, stream=True, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        self.stream_request(method, path, body, headers, append_callback, append_thinking_callback, stop_checker=stop_checker, status_callback=status_callback)

    def request_with_tools(
        self,
        messages: list[Any],
        max_tokens: int = 512,
        tools: Any = None,
        append_callback: Any = None,
        append_thinking_callback: Any = None,
        stop_checker: Any = None,
        status_callback: Any = None,
        body_override: Any = None,
        model: str | None = None,
        stream: bool = False,
        response_format: Any = None,
        chat_extra: Any = None,
        prepend_dev_build_system_prefix: bool = True,
    ) -> dict[str, Any]:
        """Chat request with support for tools and streaming.

        If stream=True, uses callbacks to stream deltas & accumulates tool_calls.
        If stream=False, makes a standard blocking call.

        Returns a dict: {role, content, tool_calls, finish_reason, images, usage}
        """
        init_logging(self.ctx)
        requested_model = model or self.config.get("model", "")
        n_tool_defs = len(tools) if isinstance(tools, list) else 0
        log.info("Sending LLM chat request: provider=%s requested_model=%r stream=%s n_messages=%d n_tool_defs=%d", self._get_provider(), requested_model, stream, len(messages), n_tool_defs)
        method, path, body, headers = self.make_chat_request(messages, max_tokens, tools=tools, stream=stream, model=model, response_format=response_format, chat_extra=chat_extra, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        if body_override is not None:
            body = body_override.encode("utf-8") if isinstance(body_override, str) else body_override

        requested_model = model or self.config.get("model") or "default"
        thinking_parts: list[str] = []
        thinking_meta = new_streaming_thinking_meta()
        message_snapshot: dict[str, Any] = {}
        content = ""
        tool_calls = None
        images: list[Any] = []
        usage: dict[str, Any] = {}
        used_model: str = requested_model

        if stream:
            append_callback = append_callback or (lambda t: None)
            append_thinking_callback = append_thinking_callback or (lambda t: None)

            def on_delta(d: dict[object, object]) -> None:
                _normalize_delta(d)
                accumulate_streaming_thinking(thinking_parts, thinking_meta, cast("dict[str, Any]", d))
                d_for_snapshot = {k: v for k, v in d.items() if k not in THINKING_DELTA_KEYS}
                accumulate_delta(message_snapshot, d_for_snapshot)
                if "model" in d and "model" not in message_snapshot:
                    message_snapshot["model"] = d["model"]

            def _reset_unemitted_attempt() -> None:
                # on_delta also fills thinking meta from a reasoning_details
                # chunk that had no display text. That is the same unsent
                # attempt. Do not call this after a token was shown.
                message_snapshot.clear()
                thinking_parts.clear()
                thinking_meta.clear()
                thinking_meta.update(new_streaming_thinking_meta())

            log.debug("stream_request_with_tools: building request (%d messages)..." % len(messages))
            try:
                last_finish_reason = self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, on_delta=on_delta, stop_checker=stop_checker, status_callback=status_callback, reset_unemitted_attempt=_reset_unemitted_attempt)
            except NetworkError:
                raise
            except Exception as e:
                if isinstance(e, WriterAgentException) or is_disposed_exception(e):
                    raise
                err_msg = format_error_message(e)
                log.exception("stream_request_with_tools failed")
                raise NetworkError(err_msg, details={"url": path}) from e

            raw_content = message_snapshot.get("content")
            normalized_content = _normalize_message_content(raw_content)
            content, extracted_thinking = strip_think_tags(normalized_content)
            if extracted_thinking and not thinking_parts:
                thinking_parts.append(extracted_thinking)
            message_snapshot["content"] = content
            tool_calls = message_snapshot.get("tool_calls")
            if tool_calls is not None:
                tool_calls = coalesce_split_tool_calls(tool_calls) or None
                message_snapshot["tool_calls"] = tool_calls
            if tool_calls is not None:
                log.debug("streaming_loop: accumulated tool_calls model=%r tool_calls=%s", requested_model, json.dumps(tool_calls, ensure_ascii=False))
            usage = cast("dict[str, Any]", message_snapshot.get("usage", {}))
            used_model = str(message_snapshot.get("model") or requested_model)
            reasoning_replay = extract_reasoning_replay_from_response(streaming_text="".join(thinking_parts), streaming_meta=thinking_meta)
        else:
            # Sync path (nested smol / specialized). Same Stop-before-connect latch as stream.
            if self._stopped or (stop_checker and stop_checker()):
                log.debug("request_with_tools sync: Stop already requested before connect")
                self._stopped = True
                self._close_connection()
                return {"role": "assistant", "content": "", "tool_calls": None, "finish_reason": "stop", "images": [], "usage": {}, "model": requested_model}
            def _log_outgoing_on_retry() -> None:
                try:
                    redacted_msgs = redact_sensitive_payload_for_log(messages)
                    log.debug("request_with_tools outgoing messages (redacted): %s", json.dumps(redacted_msgs, indent=2, ensure_ascii=False))
                except Exception as log_exc:
                    log.warning("Could not log redacted outgoing messages: %s", log_exc)

            action, result = self._exchange_json(method, path, body, headers, stop_checker=stop_checker, status_callback=status_callback, retry_log_message="Retrying request_with_tools on fresh connection", on_retry=_log_outgoing_on_retry, wrap_unexpected=True, failure_log="request_with_tools failed")
            if action == "stop":
                return {"role": "assistant", "content": "", "tool_calls": None, "finish_reason": "stop", "images": [], "usage": {}, "model": requested_model}

            if log.isEnabledFor(logging.DEBUG):
                log.debug("=== Sync response: %s" % json.dumps(redact_sensitive_payload_for_log(result), indent=2))

            used_model = str(result.get("model") or requested_model) if isinstance(result, dict) else requested_model
            log.info("LLM sync response received: provider=%s requested_model=%r used_model=%r", self._get_provider(), requested_model, used_model)

            # Use unified extraction for shims/native providers
            try:
                raw_parsed_content, last_finish_reason, tool_calls, usage, images, message = self._get_shim().parse_sync_response(result)
            except NetworkError:
                raise
            except Exception as e:
                # A malformed envelope (missing keys, wrong shape) raises
                # KeyError/TypeError/AttributeError from the shim. Wrap those
                # in NetworkError(code="BAD_RESPONSE") so callers that handle
                # NetworkError see them. WriterAgentException and disposal
                # still propagate.
                if isinstance(e, WriterAgentException) or is_disposed_exception(e):
                    raise
                err_msg = format_error_message(e)
                log.exception("parse_sync_response failed")
                raise NetworkError(err_msg, code="BAD_RESPONSE", details={"url": path}) from e
            content, extracted_thinking = strip_think_tags(raw_parsed_content)
            if extracted_thinking and "reasoning" not in message:
                message["reasoning"] = extracted_thinking
            message["content"] = content
            reasoning_replay = extract_reasoning_replay_from_response(sync_message=message)

        # Shared post-processing.
        # A model that finishes with stop plus a complete tool_calls list
        # remaps to tool_calls. Skip that when this client latched Stop
        # (_stopped): a Stop/abort that already delivered a partial tool-call
        # delta must not look like a call the UI should run. The in-loop abort
        # checker and the streaming connection-error 'stop' path both set
        # _stopped before returning.
        if last_finish_reason == "stop" and tool_calls and not self._stopped:
            last_finish_reason = "tool_calls"

        if content:
            cleaned = strip_leaked_chat_template_control_tokens(content)
            if cleaned != content:
                log.info("Stripped leaked <|...|> chat-template tokens from assistant content (model=%s, original_len=%d, cleaned_len=%d)", requested_model, len(content), len(cleaned))
                log.debug("Stripped leaked chat-template control tokens from model content. original=%r cleaned=%r", content, cleaned)
                content = cleaned

        if not tool_calls and content:
            from plugin.contrib.tool_call_parsers import get_parser_for_model

            parser = get_parser_for_model(requested_model)
            if parser:
                p_content, p_tool_calls = parser.parse(content)
                if p_tool_calls:
                    tool_calls = p_tool_calls
                    content = p_content or ""
                    if last_finish_reason != "tool_calls":
                        last_finish_reason = "tool_calls"

        if tool_calls:
            tool_calls = coalesce_split_tool_calls(tool_calls) or None

        out: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls, "finish_reason": last_finish_reason, "images": images, "usage": usage, "model": used_model}
        out.update(reasoning_replay)
        return out

    def stream_request_with_tools(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Streaming chat request with tools. Wrapper around request_with_tools."""
        kwargs["stream"] = True
        return self.request_with_tools(*args, **kwargs)

    def chat_completion_sync(
        self,
        messages: list[Any],
        max_tokens: int = 512,
        model: str | None = None,
        response_format: Any = None,
        chat_extra: Any = None,
        *,
        prepend_dev_build_system_prefix: bool = True,
        stop_checker: Any = None,
        status_callback: Any = None,
    ) -> str:
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.

        ``finish_reason == "stop"`` is a normal completion. When
        ``stop_checker`` is passed, a user stop (checker true, or this
        client's stop latch) raises ``ToolExecutionError(code="USER_STOPPED")``
        instead of ``""``. Callers that omit the checker keep the string return.
        """
        result = self.request_with_tools(
            messages,
            max_tokens=max_tokens,
            tools=None,
            model=model,
            response_format=response_format,
            chat_extra=chat_extra,
            prepend_dev_build_system_prefix=prepend_dev_build_system_prefix,
            stop_checker=stop_checker,
            status_callback=status_callback,
        )
        # request_with_tools reports Stop as an empty assistant message whose
        # finish_reason is also "stop" — the same finish_reason a normal
        # completion uses. Deep-research planning treated that "" as a finished
        # plan and later cached a partial report. Only the checker or the
        # client latch means the user stopped. No checker: grammar and forms
        # still receive the historical string, including "".
        if stop_checker is not None and (self._stopped or stop_checker()):
            from plugin.framework.errors import ToolExecutionError

            raise ToolExecutionError("LLM request stopped by user.", code="USER_STOPPED")
        return result.get("content") or ""
