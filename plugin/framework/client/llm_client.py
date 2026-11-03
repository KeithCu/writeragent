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
import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    from .base_provider_shim import BaseProviderShim

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20

from .response_normalizers import normalize_message_content, prepare_chat_messages, strip_leaked_chat_template_control_tokens

# accumulate_delta merges streaming deltas into message_snapshot; coalesce_split_tool_calls repairs OpenRouter empty-name split tool_calls.
from plugin.framework.stream_delta import accumulate_delta, coalesce_split_tool_calls
from plugin.framework.constants import USER_AGENT
from plugin.framework.send_cancellation import SendCancellation, get_current_send_cancellation, is_cancelled

from plugin.framework.logging import init_logging as init_logging, redact_sensitive_payload_for_log
from plugin.framework.client.auth import AuthError, resolve_auth_for_config, build_auth_headers
from plugin.framework.errors import NetworkError, WriterAgentException, is_disposal
from plugin.framework.url_utils import get_api_version_suffix, get_url_path_and_query, normalize_endpoint_url

from plugin.framework.errors import format_error_message
from .errors import (
    _format_http_error_response,
    _log_chat_request_body_diag,
    _log_http_500_request_diag,
    _parse_provider_envelope,
    _path_without_query,
    append_zai_unknown_model_hint,
    stream_error_is_overload,
    stream_error_message,
)
from .http_security import redact_secrets
from .http_transport import LlmHttpTransport, parse_strict_json
from .request_controls import (
    RetryBudget,
    StreamOverload,
    request_model_from_body,
)
from .stream_normalizer import (
    THINKING_DELTA_KEYS,
    ThinkTagStreamSplitter,
    accumulate_streaming_thinking,
    extract_reasoning_replay_from_response,
    iterate_sse,
    new_streaming_thinking_meta,
    normalize_delta,
    strip_think_tags,
)
from .provider_detection import is_openrouter_endpoint
from .shim_helpers import cache_read_count

log = logging.getLogger(__name__)

# Anthropic Messages SSE has no OpenAI ``choices`` array. These event types
# still carry text, tool_use, stop_reason, or usage. message_start nests
# input-token usage under message.usage; the shim reads it. ping is keepalive.
_ANTHROPIC_STREAM_TYPES = frozenset({"message_start", "content_block_start", "content_block_delta", "message_delta", "message_stop", "message"})


def _chunk_should_parse(chunk: dict[str, Any]) -> bool:
    """True when a stream JSON object should be handed to the provider shim."""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        return True
    event_type = chunk.get("type")
    if isinstance(event_type, str) and event_type.startswith("response."):
        return True
    return event_type in _ANTHROPIC_STREAM_TYPES


def _accepted_value_lists(param: str, error_text: str) -> list[list[str]] | None:
    """Accepted-value lists for one rejected param, or None when the error does not name it.

    A line with no ``Accepted:`` list is an empty list. Trailing JSON punctuation
    is excluded so ``Accepted: 1:1"}`` does not become part of the token.
    """
    pattern = rf"\b{param}\b: not supported\.(?: Accepted: ([^|}}\n\"]+))?"
    matches = list(re.finditer(pattern, error_text))
    if not matches:
        return None
    lists: list[list[str]] = []
    for match in matches:
        accepted_str = match.group(1)
        if accepted_str:
            lists.append([item.strip() for item in accepted_str.split(",") if item.strip()])
        else:
            lists.append([])
    return lists


def _common_accepted(lists: list[list[str]]) -> list[str]:
    """Values every non-empty Accepted list shares, in the first list's order.

    A line that names no values is ignored when another line does, so one
    provider omitting the list does not erase the other's hint. An empty
    result means the lists disagree or nobody named a value.
    """
    named = [items for items in lists if items]
    if not named:
        return []
    common = set(named[0])
    for items in named[1:]:
        common.intersection_update(items)
    return [item for item in named[0] if item in common]


def adjust_image_body_for_rejection(body: bytes, error_text: str) -> bytes | None:
    """Adjust an image request after ``param: not supported``.

    ``resolution``, ``size``, and ``output_format`` are dropped. Remapping
    ``512`` to the first ``Accepted:`` token could pick ``4K`` or ``webp``
    (Flux Klein rejects webp). The provider default is the value that works
    (``1K`` for gemini-3.1-flash-lite-image).

    ``aspect_ratio`` is remapped to the closest accepted ratio. Dropping it
    turns a 16:9 request into a square. Unknown tokens and disagreeing lists
    delete the field instead.

    Returns the new body bytes, or None if nothing changed.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    from .base_provider_shim import closest_named_aspect

    changed = False
    # Example: "resolution: not supported. Accepted: 1K | Google AI Studio: resolution: not supported. Accepted: 1K"
    for param in ("resolution", "aspect_ratio", "size", "output_format"):
        if param not in data:
            continue
        accepted_lists = _accepted_value_lists(param, error_text)
        if accepted_lists is None:
            continue
        if param != "aspect_ratio":
            del data[param]
            changed = True
            continue
        # Keep list order so an equal distance uses the earlier accepted token.
        new_val = closest_named_aspect(str(data.get(param) or ""), _common_accepted(accepted_lists))
        if new_val and new_val != data.get(param):
            data[param] = new_val
        else:
            # Same value, or no known ratio: a retry of the rejected body would 400 again.
            del data[param]
        changed = True

    if changed:
        return json.dumps(data).encode("utf-8")
    return None


from .openai_shim import get_provider_shim_class
from .base_provider_shim import accepted_effort_body, remember_accepted_effort


class LlmClient:
    """LLM API client. Takes config dict from get_api_config() and UNO ctx."""

    config: dict[str, Any]
    ctx: Any
    _transport: LlmHttpTransport
    _owned: SendCancellation

    def __init__(self, config: dict[str, Any], ctx: Any, cancel: SendCancellation | None = None, *, register_with_send: bool = True) -> None:
        self.config = config
        self.ctx = ctx
        self._transport = LlmHttpTransport(self._endpoint, self._timeout, openrouter_getter=lambda: bool(self.config.get("is_openrouter")))
        self._shims: dict[str, BaseProviderShim] = {}
        # Stop before the first byte: close() alone is a no-op when sock is None;
        # the worker must not open a new connection (B13 / llm_request_lane).
        self._owned = SendCancellation()
        scope = cancel
        if scope is None and register_with_send:
            scope = get_current_send_cancellation()
        if scope is not None and register_with_send:
            scope.register_client(self)

    def stopped(self) -> bool:
        return self._owned.is_cancelled()

    def _request_scope(self, cancel: SendCancellation | None) -> SendCancellation:
        """Link caller's cancellation scope with this client's owned scope.

        What was wrong: Stop could fire after _activate returned a caller scope
        but before _inflight was assigned. stop() cancelled _owned only, leaving
        the active scope uncancelled so reconnect was attempted.
        Fix: register a late-cancellation hook directly on _owned so stop() cancels
        both the client and caller scope in a single pass without needing _inflight.
        """
        owned = self._owned
        if cancel is None or cancel is owned:
            return owned
        # stop() cancels owned. The hook below cancels the caller's scope in that
        # same cancel(), including when stop() already ran (late hook).
        # Do not register self.stop on cancel: scope.cancel() must not latch the client.
        owned.register_on_cancel(cancel.cancel)
        if owned.is_cancelled():
            return owned
        return cancel

    def _release_request_scope(self, cancel: SendCancellation | None) -> None:
        """Unregister caller cancellation hook when request ends."""
        if cancel is not None and cancel is not self._owned:
            self._owned.unregister_on_cancel(cancel.cancel)

    def _effort_on_bad_request(self, scope: SendCancellation, slot: dict[str, Any]) -> Callable[[bytes], bytes | None]:
        """Build bad-request callback for effort 400 retries.

        What was wrong: the 400-effort retry callback was duplicated between
        _exchange_json and _run_streaming_loop.
        Fix: single helper closure updating the caller's mutable slot dict.
        """
        def on_bad_request(body_bytes: bytes) -> bytes | None:
            if scope.is_cancelled():
                return None
            if slot["pending"] is None:
                from .base_provider_shim import effort_retry_bodies

                slot["pending"] = effort_retry_bodies(self._get_shim()._reasoning_provider(), body_bytes)
            if not slot["pending"]:
                return None
            log.warning("HTTP 400 rejected reasoning effort; retrying with another effort")
            slot["used"] = True
            next_body = slot["pending"].pop(0)
            slot["body"] = next_body
            return next_body

        return on_bad_request

    def _get_shim(self) -> BaseProviderShim:
        """Get the provider shim for this client."""
        provider = self._get_provider()
        endpoint = self._endpoint()
        shim_key = f"{provider}:{endpoint}"
        if shim_key not in self._shims:
            shim_cls = get_provider_shim_class(provider)
            self._shims[shim_key] = shim_cls(self)
        return self._shims[shim_key]

    def _close_connection(self) -> None:
        self._transport.close()

    def _close_if_connection_close(self, response: Any) -> None:
        """Drop the keep-alive socket when the server sent ``Connection: close``.

        Call only after the body is fully read. Stop already closed the socket;
        a later ``read()`` would block until ``request_timeout`` and hold
        ``llm_request_lane``.
        """
        if self.stopped() or not hasattr(response, "getheader"):
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
        safe_path = redact_secrets(_path_without_query(path), [api_key])
        st_raw = getattr(response, "status", 500)
        try:
            st = int(st_raw)
        except (TypeError, ValueError):
            st = 500
        log.error("Provider API Error %s: %s (provider=%s path=%s request_model=%r)", str(st_raw), redact_secrets(str(err_body), [api_key]), self._get_provider(), safe_path, request_model)
        n_ctx = None
        if st == 500:
            from .model_fetcher import peek_live_ollama_num_ctx

            n_ctx = peek_live_ollama_num_ctx(self)
        if st == 500:
            _log_http_500_request_diag(self, response, path, body, err_body, n_ctx=n_ctx)
        err_msg = _format_http_error_response(st, str(getattr(response, "reason", "") or ""), str(err_body), context_window=n_ctx)
        return append_zai_unknown_model_hint(err_msg, err_body, path, self._get_provider(), request_model)

    def _wire_secrets(self) -> list[str]:
        api_key = str(self.config.get("api_key") or "").strip()
        return [api_key] if api_key else []

    def stop(self) -> None:
        """Abort the in-flight request: latch + close socket (even if not open yet).

        Packet B13: Stop can fire before ``get_connection``. Without a latched
        scope, the worker opens a fresh socket and holds ``llm_request_lane`` until timeout.
        """
        log.debug("LlmClient.stop() called")
        self._owned.cancel()
        self._close_connection()

    def clear_stop(self) -> None:
        """Allow a reused client to send again (call on the UI thread at send start)."""
        self._owned = SendCancellation()

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

        return h

    def _resolve_auth(self) -> dict[str, Any]:
        """Resolve auth info from config, propagating any AuthError."""
        return resolve_auth_for_config(self.config)

    def _get_provider(self) -> str:
        """Get the provider ID from resolved auth."""
        auth_info = self._resolve_auth()
        return auth_info.get("provider", "custom")

    def _timeout(self) -> Any:
        """Settings read/stall budget. Connect uses ``LLM_CONNECT_TIMEOUT_SEC``."""
        return self.config.get("request_timeout", 120)

    def _send_request(self, method: str, path: str, body: Any, headers: dict[str, str], *, cancel: SendCancellation | None = None, status_callback: Any = None) -> Any:
        """Send through the transport with cancellation scope."""
        active = self._request_scope(cancel)
        try:
            if is_cancelled(active):
                raise NetworkError("LLM request aborted by Stop", code="STOPPED")

            return self._transport.send(method, path, body, headers, cancel=active, status_callback=status_callback)
        finally:
            self._release_request_scope(cancel)

    def make_chat_request(
        self,
        messages: list[Any],
        max_tokens: int = 512,
        tools: Any = None,
        stream: bool = False,
        model: str | None = None,
        response_format: Any = None,
        chat_extra: Any = None,
        tool_choice: str | None = None,
        *,
        prepend_dev_build_system_prefix: bool = True,
    ) -> tuple[str, str, Any, dict[str, str]]:
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
        method, path, body, headers = shim.build_chat_request(
            messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra, tool_choice=tool_choice
        )

        log.debug("=== Chat Request (provider=%s, tools=%s, stream=%s) ===" % (self._get_provider(), bool(tools), stream))
        log.debug("URL: %s" % _path_without_query(path))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Messages: %s" % json.dumps(redact_sensitive_payload_for_log(messages), indent=2))
        _log_chat_request_body_diag(self, path, body, headers, tools)

        return method, path, body, headers

    def _exchange_json(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str],
        *,
        cancel: SendCancellation | None = None,
        status_callback: Any = None,
        retry_log_message: str,
        on_retry: Any = None,
        wrap_unexpected: bool = False,
        failure_log: str = "JSON request failed",
    ) -> tuple[str, Any]:
        """One non-streaming JSON exchange with the shared retry budget."""
        active = self._request_scope(cancel)
        try:
            if is_cancelled(active):
                self._close_connection()
                return "stop", None
            api_key = str(self.config.get("api_key") or "").strip()
            first_body = body if isinstance(body, (bytes, bytearray)) else body
            slot: dict[str, Any] = {"pending": None, "body": body, "used": False}
            on_bad_request = self._effort_on_bad_request(active, slot)

            try:
                # Same exchange as catalog and speech. Stop, timeout, retry, and
                # redaction are not a second loop here.
                result = self._transport.exchange(
                    method,
                    path,
                    body,
                    headers,
                    cancel=active,
                    status_callback=status_callback,
                    parse_json=False,
                    sender=self._send_request,
                    secrets=self._wire_secrets(),
                    on_retry=on_retry,
                    after_read=self._close_if_connection_close,
                    observe_http_error=self._observe_provider_http_error,
                    retry_log_message=retry_log_message,
                    on_bad_request=on_bad_request,
                )
            except NetworkError as exc:
                if getattr(exc, "code", None) == "STOPPED":
                    return "stop", None
                raise
            except Exception as exc:
                if not wrap_unexpected:
                    raise
                err_msg = format_error_message(exc)
                log.exception(failure_log)
                raise NetworkError(err_msg, details={"url": path}) from exc
            if slot["used"] and isinstance(first_body, (bytes, bytearray)):
                fb = bytes(first_body) if isinstance(first_body, bytearray) else first_body
                lb = bytes(slot["body"]) if isinstance(slot["body"], bytearray) else slot["body"]
                remember_accepted_effort(self._endpoint(), fb, lb)
            return "ok", _parse_provider_envelope(result.body, path, api_key=api_key)
        finally:
            self._release_request_scope(cancel)

    def _request_json(self, method: str, path: str, body: Any, headers: dict[str, str], *, cancel: SendCancellation | None = None, status_callback: Any = None) -> Any:
        """Blocking JSON call on the persistent transport.

        Image and speech used ``sync_request``, which ignored Stop and 429/503
        backoff. This shares the chat transport so ``stop()`` closes the socket.
        ``cancel`` / ``status_callback`` match the sync chat path: a 429
        wait ends when the caller stops, and the sidebar sees the retry line.
        """
        action, parsed = self._exchange_json(method, path, body, headers, cancel=cancel, status_callback=status_callback, retry_log_message="Retrying JSON request on fresh connection")
        if action == "stop":
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        return parsed

    def image_completion(self, prompt: str, model: str | None = None, width: int = 1024, height: int = 1024, steps: int | None = None, source_image: str | None = None, image_url: str | None = None, *, cancel: SendCancellation | None = None, status_callback: Any = None) -> Any:
        """Generate images using the configured provider. Returns list of base64 strings."""
        method, path, body, headers = self._get_shim().build_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)
        endpoint = self._endpoint()
        if path.startswith("/"):
            parsed = urllib.parse.urlparse(endpoint)
            url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        else:
            url = path

        # log.debug...
        log.debug("=== Image Request ===")
        # Path/query must not include API keys.
        log.debug("URL: %s" % urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(query="", fragment="")))

        # Persistent transport supports cancellation and 429/503 retries.
        try:
            res = self._request_json(method, path, body, headers, cancel=cancel, status_callback=status_callback)
        except NetworkError as e:
            if "not supported" in str(e):
                new_body = adjust_image_body_for_rejection(body, str(e))
                if new_body and not is_cancelled(cancel):
                    # The adjusted body is multi-MB base64. Log a 500-character
                    # preview plus the total length, not the whole payload.
                    body_text = new_body.decode("utf-8", errors="replace")
                    body_preview = f"{body_text[:500]}... [total {len(body_text)} chars]" if len(body_text) > 500 else body_text
                    log.warning("Image API rejected params, retrying with adjusted body: %s", body_preview)
                    res = self._request_json(method, path, new_body, headers, cancel=cancel, status_callback=status_callback)
                else:
                    raise
            else:
                raise

        if not res:
            return []

        shim = self._get_shim()
        return shim.parse_image_responses(res)

    def transcribe_audio(self, wav_path: str, model: str | None = None, *, cancel: SendCancellation | None = None, status_callback: Any = None) -> str:
        """Transcribe audio via POST /v1/audio/transcriptions (or chat if STT model supports input_audio).

        STT-only models use the transcription endpoint only; chat+audio STT models may
        try chat completions first. See docs/chat/audio-architecture.md.
        """
        import uuid
        import os
        import base64
        from plugin.framework.client.model_fetcher import has_native_audio

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
                    cancel=cancel,
                    status_callback=status_callback,
                )
            except AuthError:
                raise
            except NetworkError as e:
                if self.stopped() or getattr(e, "code", None) == "STOPPED":
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
        res = self._request_json("POST", req_path, body_bytes, headers, cancel=cancel, status_callback=status_callback)
        return res.get("text", "") if isinstance(res, dict) else str(res)

    def stream_completion(self, prompt: str, system_prompt: str, max_tokens: int, append_callback: Any, append_thinking_callback: Any = None, cancel: SendCancellation | None = None, status_callback: Any = None) -> None:
        """Stream a chat completions response via callbacks."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        self.stream_chat_response(messages, max_tokens, append_callback, append_thinking_callback, cancel=cancel, status_callback=status_callback)

    def _run_streaming_loop(self, method: str, path: str, body: Any, headers: dict[str, str], on_content: Any, on_thinking: Any = None, on_delta: Any = None, cancel: SendCancellation | None = None, status_callback: Any = None, reset_unemitted_attempt: Any = None) -> Any:
        """Common low-level streaming engine."""
        log.info("=== Starting streaming loop (persistent) ===")
        log.debug("Request Path: %s" % path)

        # Do not clear cancellation here — that races with stop() on another thread
        # (B13). UI clears via ``clear_stop()`` at the start of a new send.
        active = self._request_scope(cancel)
        if active.is_cancelled():
            log.debug("streaming_loop: Stop already requested before connect")
            self._close_connection()
            self._release_request_scope(cancel)
            return "stop"

        first_body = body
        slot: dict[str, Any] = {"pending": None, "body": body, "used": False}
        on_bad_request = self._effort_on_bad_request(active, slot)

        budget = RetryBudget(
            self._transport,
            cancel=active,
            status_callback=status_callback,
            on_bad_request=on_bad_request,
        )
        prepared = accepted_effort_body(
            self._get_shim()._reasoning_provider(),
            self._endpoint(),
            bytes(body) if isinstance(body, bytearray) else body,
        )
        body = prepared if isinstance(prepared, bytes) else prepared.encode("utf-8")

        def consume(response: Any) -> str | None:
            # on_delta writes role, usage, and a buffered "<think" prefix into
            # the caller's snapshot before any callback runs. emitted_any stays
            # false for a usage-only chunk and a partial tag, so an overload or
            # connection retry would call accumulate_delta again and add
            # prompt_tokens / completion_tokens and concatenate that prefix.
            # Drop only that attempt, and only while nothing has been shown.
            # Once emitted_any is set this does not run, so shown text stays.
            # The first call clears an empty snapshot; a later call drops the
            # failed attempt before the next send.
            if not budget.emitted_any and reset_unemitted_attempt is not None:
                reset_unemitted_attempt()
            last_finish_reason: str | None = None
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
                stream_state: dict[str, Any] = {}
                last_usage_obj: dict[str, Any] | None = None

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

                    # stop() cancels the scope and closes the socket, but http.client
                    # can already have buffered SSE lines. Checking is_cancelled here
                    # avoids parsing buffered lines and passing them to on_content.
                    if active.is_cancelled():
                        log.debug("streaming_loop: Stop requested.")
                        last_finish_reason = "stop"
                        content_finished = True
                        # Close connection immediately on stop so response.read() does not block.
                        self._close_connection()
                        break

                    # Check for mid-stream provider error objects inside a 200 stream.
                    stream_err = stream_error_message(chunk)
                    if stream_err is not None:
                        api_key = str(self.config.get("api_key") or "").strip()
                        stream_err = redact_secrets(stream_err, [api_key])
                        if (not budget.emitted_any) and stream_error_is_overload(chunk) and budget.sends_left > 1:
                            self._close_connection()
                            raise StreamOverload(stream_err)
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

                    content, finish_reason, thinking, delta = self._get_shim().parse_response_chunk(chunk, stream_state)
                    # Keep the provider's parsed SSE shape before normalization. Some
                    # OpenAI-compatible routes have appeared to continue one function's
                    # arguments under a new tool-call index; the completed snapshot alone
                    # cannot tell whether that index came from the wire or our accumulator.
                    raw_tool_calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                    if raw_tool_calls is not None:
                        log.debug(
                            "streaming_loop: raw tool_call delta route_provider=%s chunk_provider=%r chunk_model=%r chunk_id=%r tool_calls=%s",
                            self._get_provider(),
                            chunk.get("provider"),
                            chunk.get("model"),
                            chunk.get("id"),
                            json.dumps(raw_tool_calls, ensure_ascii=False),
                        )

                    # LiteLLM: streaming_handler.py ~L736 "finish_reason: error, no content string given"
                    if finish_reason == "error":
                        from plugin.framework.i18n import _

                        raise NetworkError(_("Stream ended with finish_reason=error"), code="STREAM_ERROR")

                    if thinking:
                        # A reasoning delta still reaches on_delta even when
                        # on_thinking is missing. Count those bytes, or a
                        # socket drop retries text the UI already has.
                        budget.note_emitted()
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
                                    budget.note_emitted()
                                    if on_thinking:
                                        on_thinking(text_piece)
                            else:
                                if text_piece:
                                    budget.note_emitted()
                                if on_content:
                                    on_content(text_piece)
                                # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                                last_contents.append(text_piece)
                                if (
                                    len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT
                                    and len(text_piece) > 2
                                    and all(c == last_contents[0] for c in last_contents)
                                ):
                                    from plugin.framework.i18n import _

                                    raise NetworkError(
                                        _("The model is repeating the same chunk (infinite loop). Try again or use a different model."),
                                        code="INFINITE_LOOP",
                                    )
                    if raw_tool_calls:
                        # Tool-call deltas still call on_delta, and
                        # accumulate_delta concatenates function.name and
                        # arguments onto a snapshot created outside the
                        # retry loop. A socket drop after those chunks would
                        # merge a second copy of the call. Count tool-call
                        # bytes like emitted text so the retry branch raises
                        # CONNECTION_LOST. Do not clear or replay the
                        # snapshot; callbacks have already run.
                        budget.note_emitted()
                    outgoing: dict[str, Any] | None = delta if isinstance(delta, dict) else None
                    # Preserve normalized usage already present on the delta before checking chunk usage.
                    shim_usage = outgoing.get("usage") if isinstance(outgoing, dict) else None
                    if usage_obj and not (isinstance(shim_usage, dict) and shim_usage):
                        outgoing = dict(outgoing or {})
                        outgoing["usage"] = usage_obj
                    cur_usage = outgoing.get("usage") if isinstance(outgoing, dict) else None
                    if not isinstance(cur_usage, dict):
                        cur_usage = usage_obj
                    if isinstance(cur_usage, dict):
                        if last_usage_obj is None:
                            last_usage_obj = dict(cur_usage)
                        else:
                            last_usage_obj.update(cur_usage)
                    if outgoing and on_delta:
                        # on_delta receives this normalized delta; do not normalize twice.
                        normalize_delta(outgoing)
                        if chunk_model and "model" not in outgoing:
                            outgoing["model"] = str(chunk_model)
                        on_delta(outgoing)

                    if active.is_cancelled():
                        log.debug("streaming_loop: Stop requested after delta.")
                        last_finish_reason = "stop"
                        content_finished = True
                        self._close_connection()
                        break

                    if finish_reason:
                        log.debug("streaming_loop: logical finish_reason=%s" % finish_reason)
                        last_finish_reason = finish_reason

                # iterate_sse finishes quietly on EOF, so a socket that
                # closed early looked like a successful answer with
                # finish_reason None. A missing [DONE] / finish_reason
                # is a drop. Raise a real connection error so the outer
                # loop can retry (if nothing was emitted) or surface
                # CONNECTION_LOST.
                if not content_finished and not last_finish_reason:
                    if active.is_cancelled():
                        last_finish_reason = "stop"
                        content_finished = True
                    else:
                        raise ConnectionResetError("Stream truncated without [DONE] or finish_reason")

                log.info(
                    "LLM response stream finished: provider=%s requested_model=%r used_model=%r finish_reason=%s cache_read=%s",
                    self._get_provider(),
                    requested_model,
                    used_model or requested_model,
                    last_finish_reason,
                    cache_read_count(last_usage_obj),
                )

                # Flush any trailing buffered text from the think tag splitter
                # (trailing buffer contains small tag prefix remnants like '<' at EOF).
                # The splitter holds at most a tag prefix. Flushing after Stop
                # would still call on_content for a partial "<think". A
                # completed stream still needs the flush; Stop must not
                # paint that prefix into the sidebar.
                if not active.is_cancelled():
                    for is_think, text_piece in think_tag_splitter.flush():
                        if is_think and text_piece:
                            budget.note_emitted()
                            if on_thinking:
                                on_thinking(text_piece)
                        elif not is_think and on_content:
                            if text_piece:
                                budget.note_emitted()
                            on_content(text_piece)
                clean_finish = not active.is_cancelled()
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
                        log.debug("stream trailing read failed", exc_info=True)
                    # Honor Connection: close so we don't try to reuse when the server closed.
                    self._close_if_connection_close(response)
                else:
                    try:
                        self._close_connection()
                    except Exception:
                        log.debug("stream close after non-clean finish failed", exc_info=True)
            if slot["pending"] is not None:
                remember_accepted_effort(self._endpoint(), first_body, slot["body"])
            if last_finish_reason is None and self.stopped():
                last_finish_reason = "stop"
            return last_finish_reason

        try:
            return budget.run_stream(method, path, body, headers, consume, wire_secrets=self._wire_secrets())
        except NetworkError as e:
            if getattr(e, "code", None) == "STOPPED":
                return "stop"
            raise
        except Exception as e:
            if isinstance(e, WriterAgentException) or is_disposal(e):
                raise
            err_msg = format_error_message(e)
            log.exception("streaming_loop: Unexpected error")
            raise NetworkError(err_msg, details={"url": path}) from e
        finally:
            self._release_request_scope(cancel)


    def stream_chat_response(
        self,
        messages: list[Any],
        max_tokens: int,
        append_callback: Any,
        append_thinking_callback: Any = None,
        cancel: SendCancellation | None = None,
        status_callback: Any = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        prepend_dev_build_system_prefix: bool = True,
    ) -> None:
        """Stream a chat response using the messages array."""
        method, path, body, headers = self.make_chat_request(
            messages,
            max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
            prepend_dev_build_system_prefix=prepend_dev_build_system_prefix,
        )
        self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, cancel=cancel, status_callback=status_callback)

    def request_with_tools(
        self,
        messages: list[Any],
        max_tokens: int = 512,
        tools: Any = None,
        append_callback: Any = None,
        append_thinking_callback: Any = None,
        cancel: SendCancellation | None = None,
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
        requested_model = model or self.config.get("model") or "default"
        n_tool_defs = len(tools) if isinstance(tools, list) else 0
        log.info("Sending LLM chat request: provider=%s requested_model=%r stream=%s n_messages=%d n_tool_defs=%d", self._get_provider(), requested_model, stream, len(messages), n_tool_defs)
        method, path, body, headers = self.make_chat_request(messages, max_tokens, tools=tools, stream=stream, model=model, response_format=response_format, chat_extra=chat_extra, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        if body_override is not None:
            body = body_override.encode("utf-8") if isinstance(body_override, str) else body_override

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
                accumulate_streaming_thinking(thinking_parts, thinking_meta, cast("dict[str, Any]", d))
                d_for_snapshot = {k: v for k, v in d.items() if k not in THINKING_DELTA_KEYS}
                # Anthropic message_delta.usage is a cumulative total (platform
                # streaming docs: message_start.output_tokens is 1, the final
                # event is 15). accumulate_delta adds numbers, so those became
                # 16. Replace each usage key instead of summing it. One OpenAI
                # usage chunk still lands unchanged.
                usage_update = d_for_snapshot.pop("usage", None)
                accumulate_delta(message_snapshot, d_for_snapshot)
                if isinstance(usage_update, dict) and usage_update:
                    prev_usage = message_snapshot.get("usage")
                    merged_usage = dict(prev_usage) if isinstance(prev_usage, dict) else {}
                    merged_usage.update(cast("dict[str, Any]", usage_update))
                    message_snapshot["usage"] = merged_usage
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
                last_finish_reason = self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, on_delta=on_delta, cancel=cancel, status_callback=status_callback, reset_unemitted_attempt=_reset_unemitted_attempt)
            except NetworkError:
                raise
            except Exception as e:
                if isinstance(e, WriterAgentException) or is_disposal(e):
                    raise
                err_msg = format_error_message(e)
                log.exception("stream_request_with_tools failed")
                raise NetworkError(err_msg, details={"url": path}) from e
            if last_finish_reason is None and cancel is not None and cancel.is_cancelled():
                last_finish_reason = "stop"
            if last_finish_reason is None and self.stopped():
                last_finish_reason = "stop"

            raw_content = message_snapshot.get("content")
            normalized_content = normalize_message_content(raw_content)
            content, extracted_thinking = strip_think_tags(normalized_content)
            if extracted_thinking and not thinking_parts:
                thinking_parts.append(extracted_thinking)
            tool_calls = message_snapshot.get("tool_calls")
            if tool_calls is not None:
                tool_calls = coalesce_split_tool_calls(tool_calls) or None
            if tool_calls is not None:
                log.debug("streaming_loop: accumulated tool_calls model=%r tool_calls=%s", requested_model, json.dumps(tool_calls, ensure_ascii=False))
            usage = cast("dict[str, Any]", message_snapshot.get("usage", {}))
            used_model = str(message_snapshot.get("model") or requested_model)
            reasoning_replay = extract_reasoning_replay_from_response(streaming_text="".join(thinking_parts), streaming_meta=thinking_meta)
        else:
            # Sync path (nested smol / specialized). Same Stop-before-connect latch as stream.
            stopped_result: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": None, "finish_reason": "stop", "images": [], "usage": {}, "model": requested_model}
            if (cancel is not None and cancel.is_cancelled()) or self.stopped():
                log.debug("request_with_tools sync: Stop already requested before connect")
                self._close_connection()
                return stopped_result
            def _log_outgoing_on_retry() -> None:
                try:
                    redacted_msgs = redact_sensitive_payload_for_log(messages)
                    log.debug("request_with_tools outgoing messages (redacted): %s", json.dumps(redacted_msgs, indent=2, ensure_ascii=False))
                except Exception as log_exc:
                    log.warning("Could not log redacted outgoing messages: %s", log_exc)

            body = accepted_effort_body(self._get_shim()._reasoning_provider(), self._endpoint(), body)
            action, result = self._exchange_json(
                method,
                path,
                body,
                headers,
                cancel=cancel,
                status_callback=status_callback,
                retry_log_message="Retrying request_with_tools on fresh connection",
                on_retry=_log_outgoing_on_retry,
                wrap_unexpected=True,
                failure_log="request_with_tools failed",
            )
            if action == "stop":
                return stopped_result

            if log.isEnabledFor(logging.DEBUG):
                log.debug("=== Sync response: %s" % json.dumps(redact_sensitive_payload_for_log(result), indent=2))

            used_model = str(result.get("model") or requested_model) if isinstance(result, dict) else requested_model

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
                if isinstance(e, WriterAgentException) or is_disposal(e):
                    raise
                err_msg = format_error_message(e)
                log.exception("parse_sync_response failed")
                raise NetworkError(err_msg, code="BAD_RESPONSE", details={"url": path}) from e

            log.info("LLM sync response received: provider=%s requested_model=%r used_model=%r cache_read=%s", self._get_provider(), requested_model, used_model, cache_read_count(usage))
            content, extracted_thinking = strip_think_tags(raw_parsed_content)
            if extracted_thinking and "reasoning" not in message:
                message["reasoning"] = extracted_thinking
            message["content"] = content
            reasoning_replay = extract_reasoning_replay_from_response(sync_message=message)

        # Shared post-processing.
        # A model that finishes with stop plus a complete tool_calls list
        # remaps to tool_calls. Skip that when the request scope is cancelled
        # (caller cancel= or this client's owned scope): a Stop that already
        # delivered a partial tool-call delta must not look like a call the UI
        # should run.
        request_cancelled = (cancel is not None and cancel.is_cancelled()) or self.stopped()
        if last_finish_reason == "stop" and tool_calls:
            if request_cancelled:
                tool_calls = None
            else:
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
        cancel: SendCancellation | None = None,
        status_callback: Any = None,
    ) -> str:
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.

        ``finish_reason == "stop"`` is a normal completion. When
        a cancellation scope is passed, a user stop raises
        ``ToolExecutionError(code="USER_STOPPED")`` instead of ``""``.
        Callers that omit the scope keep the string return.
        """
        scope = cancel
        result = self.request_with_tools(
            messages,
            max_tokens=max_tokens,
            tools=None,
            model=model,
            response_format=response_format,
            chat_extra=chat_extra,
            prepend_dev_build_system_prefix=prepend_dev_build_system_prefix,
            cancel=cancel,
            status_callback=status_callback,
        )
        # request_with_tools reports Stop as an empty assistant message whose
        # finish_reason is also "stop" — the same finish_reason a normal
        # completion uses. Deep-research planning treated that "" as a finished
        # plan and later cached a partial report. Only the cancel scope or the
        # client latch means the user stopped. No cancel scope: grammar and forms
        # still receive the historical string, including "".
        if scope is not None and (scope.is_cancelled() or self.stopped()):
            from plugin.framework.errors import ToolExecutionError

            raise ToolExecutionError("LLM request stopped by user.", code="USER_STOPPED")
        return result.get("content") or ""
