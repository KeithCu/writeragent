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
"""
LLM API client for WriterAgent.
Takes a config dict (from plugin.framework.config.get_api_config) and UNO ctx.
"""

import logging
import collections
import copy
import json
import re
import time
import urllib.parse
import http.client
import socket
import datetime
from typing import Any, cast

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20

# Minimum wall time between consecutive ``conn.request`` calls on one client (bursty
# sub-agents / tool loops can otherwise hit provider rate limits).
_LLM_MIN_REQUEST_INTERVAL_SEC = 0.05

# Local / Harmony-style models sometimes leak chat-template control tokens like
# ``<|channel|>`` into completion text. If that text is replayed on the next
# request, the server can reject the input. Strip only ``<|alphanumeric_underscore|>``-style
# tokens (not ``<tool_call>`` etc.).
_CHAT_TEMPLATE_CONTROL_TOKEN_RE = re.compile(r"<\|[a-zA-Z0-9_]+\|>")


def strip_leaked_chat_template_control_tokens(content: str | None) -> str:
    """Remove ``<|name|>`` chat-template tokens that models sometimes emit in plain text."""
    if not content:
        return ""
    return _CHAT_TEMPLATE_CONTROL_TOKEN_RE.sub("", content).strip()


def _prepend_dev_build_system_prefix_to_messages(messages: list) -> None:
    """If this is a non-release bundle, prepend a dev-oriented line to the first str system message."""
    if not should_prepend_dev_llm_system_prefix():
        return
    prefix = LLM_DEV_BUILD_SYSTEM_PREFIX
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if not isinstance(c, str):
            continue
        if c.startswith(prefix):
            return
        m["content"] = f"{prefix}\n\n{c}"
        return


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


# accumulate_delta is required for tool-calling: it merges streaming deltas into message_snapshot so full tool_calls (with function.arguments) are available.
from plugin.framework.async_stream import accumulate_delta
from plugin.framework.constants import APP_REFERER, APP_TITLE, LLM_DEV_BUILD_SYSTEM_PREFIX, should_prepend_dev_llm_system_prefix

from plugin.framework.logging import init_logging, redact_sensitive_payload_for_log
from plugin.framework.client.auth import resolve_auth_for_config, build_auth_headers, AuthError
from plugin.framework.errors import NetworkError
from plugin.framework.config import get_url_hostname, get_url_path_and_query, get_api_version_suffix

from .errors import format_error_message, _format_http_error_response
from .ssl_helpers import get_unverified_ssl_context, get_verified_ssl_context, _is_certificate_verify_error, _is_local_host
from .stream_normalizer import iterate_sse, _extract_thinking_from_delta, _normalize_message_content, _normalize_delta
from .requests import sync_request

log = logging.getLogger(__name__)


class BaseProviderShim:
    """Base class for provider-specific shims (Anthropic, Google, OpenAI)."""

    def __init__(self, client: "LlmClient"):
        self.client = client

    def build_chat_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format):
        raise NotImplementedError()

    def parse_response_chunk(self, chunk):
        """Extract content, finish_reason, thinking, and delta from a response chunk."""
        raise NotImplementedError()

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        """Build an image generation request."""
        raise NotImplementedError()

    def parse_image_responses(self, response_data):
        """Extract list of base64 image data from response."""
        raise NotImplementedError()


class OpenAIShim(BaseProviderShim):
    """Shim for OpenAI-compatible providers."""

    def build_chat_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format):
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/chat/completions"

        data = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.9, "stream": stream}
        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            data["parallel_tool_calls"] = False
        if response_format:
            data["response_format"] = response_format

        if self.client.config.get("is_openrouter"):
            extra = self.client.config.get("openrouter_chat_extra")
            if isinstance(extra, dict) and extra:
                merge_openrouter_chat_extra(data, extra)

        json_data = json.dumps(data).encode("utf-8")
        path = get_url_path_and_query(url)
        return "POST", path, json_data, self.client._headers()

    def parse_response_chunk(self, chunk):
        choices = chunk.get("choices", [])
        choice = choices[0] if choices else {}
        delta = choice.get("delta", {})

        finish_reason = choice.get("finish_reason") if choice else None
        if not finish_reason:
            finish_reason = chunk.get("finish_reason")
        if not finish_reason and choices:
            for c in choices:
                if isinstance(c, dict) and c.get("finish_reason"):
                    finish_reason = c.get("finish_reason")
                    break

        content = (delta.get("content") or "") if delta else ""
        thinking = _extract_thinking_from_delta(chunk)
        return content, finish_reason, thinking, delta

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images/generations"
        data = {"prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
        if model:
            data["model"] = model
        if steps:
            data["steps"] = steps

        # img2img extension for Together/Fal/Replicate
        if image_url:
            data["image_url"] = image_url
        elif source_image:
            if source_image.startswith("data:image"):
                data["image_url"] = source_image
            else:
                data["image_url"] = "data:image/png;base64," + source_image

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data):
        # OpenAI returns {"data": [{"b64_json": "<base64>", ...}, ...]}
        items = response_data.get("data", [])
        out = []
        for it in items:
            if b64 := it.get("b64_json"):
                out.append(b64)
        return out


class OllamaShim(OpenAIShim):
    """Shim for Ollama specifically (handles native /api endpoints if needed)."""

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        endpoint = self.client._endpoint()
        url = f"{endpoint}/api/generate"
        # Ollama native generation (e.g. for flux, sdxl, stable-diffusion)
        # Keywords from C++: flux, stable-diffusion, sdxl, dall, wuerstchen, kandinsky, dreamshaper, playground, juggernaut
        eff_model = model
        if not eff_model:
            eff_model = "flux"

        data = {"model": eff_model, "prompt": prompt, "stream": False}
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data):
        # Ollama returns {"images": ["<base64>"]}
        images = response_data.get("images")
        if images and isinstance(images, list):
            return images
        # Some models use "image" (single string)
        if img := response_data.get("image"):
            return [img]
        # Fallback to OpenAI-style if they are using the /v1/images/generations proxy in Ollama
        if "data" in response_data:
            return super().parse_image_responses(response_data)
        return []


class LlmClient:
    """LLM API client. Takes config dict from get_api_config(ctx) and UNO ctx."""

    def __init__(self, config, ctx):
        self.config = config
        self.ctx = ctx
        self._persistent_conn = None
        self._conn_key = None  # (scheme, host, port)
        self._ssl_fallback_hosts = set()
        self._last_llm_request_sent_monotonic = 0.0
        self._shims: dict[str, BaseProviderShim] = {}

    def _get_shim(self) -> BaseProviderShim:
        """Get the provider shim for this client."""
        provider = self._get_provider()
        if provider not in self._shims:
            if provider == "anthropic":
                from .anthropic_shim import AnthropicShim
                self._shims[provider] = AnthropicShim(self)
            elif provider == "google":
                from .google_shim import GoogleShim
                self._shims[provider] = GoogleShim(self)
            elif provider == "xai":
                from .grok_shim import GrokShim
                self._shims[provider] = GrokShim(self)
            elif provider == "ollama":
                self._shims[provider] = OllamaShim(self)
            else:
                self._shims[provider] = OpenAIShim(self)
        return self._shims[provider]

    def _pace_before_llm_request(self) -> None:
        """Sleep if needed so consecutive HTTP sends on this client are not back-to-back."""
        now = time.monotonic()
        wait = _LLM_MIN_REQUEST_INTERVAL_SEC - (now - self._last_llm_request_sent_monotonic)
        if wait > 0:
            time.sleep(wait)

    def _mark_llm_request_sent(self) -> None:
        self._last_llm_request_sent_monotonic = time.monotonic()

    def _get_connection(self):
        """Get or create a persistent http.client connection."""
        endpoint = self._endpoint()
        parsed = urllib.parse.urlparse(endpoint)
        scheme = parsed.scheme.lower()
        host = get_url_hostname(endpoint)
        port = parsed.port

        # Default ports if not specified
        if not port:
            port = 443 if scheme == "https" else 80

        ssl_mode = "plain"
        if scheme == "https":
            ssl_mode = "unverified"
            if _is_local_host(host) and host not in self._ssl_fallback_hosts:
                ssl_mode = "verified"
        new_key = (scheme, host, port, ssl_mode)

        if self._persistent_conn:
            if self._conn_key != new_key:
                log.debug("Closing old connection to %s, opening new to %s" % (self._conn_key, new_key))
                self._persistent_conn.close()
                self._persistent_conn = None
            else:
                return self._persistent_conn

        log.debug("Opening new connection to %s://%s:%s" % (scheme, host, port))
        self._conn_key = new_key
        timeout = self._timeout()

        if scheme == "https":
            ssl_context = get_verified_ssl_context() if ssl_mode == "verified" else get_unverified_ssl_context()
            self._persistent_conn = http.client.HTTPSConnection(host, port, context=ssl_context, timeout=timeout)
        else:
            self._persistent_conn = http.client.HTTPConnection(host, port, timeout=timeout)

        return self._persistent_conn

    def _close_connection(self):
        if self._persistent_conn:
            try:
                log.debug("Closing persistent connection to %s" % (self._conn_key,))
                # Try to shut down the actual socket to break blocking reads in other threads
                try:
                    sock = getattr(self._persistent_conn, "sock", None)
                    if sock:
                        import socket

                        sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                self._persistent_conn.close()
            except Exception:
                pass
            self._persistent_conn = None
            self._conn_key = None

    def stop(self):
        """Immediately stop any active request by closing the connection."""
        log.debug("LlmClient.stop(, level=logging.DEBUG) called")
        self._close_connection()

    def _endpoint(self):
        return self.config.get("endpoint", "http://127.0.0.1:5000")

    def _api_path(self):
        return get_api_version_suffix(self._endpoint(), is_openwebui=self.config.get("is_openwebui"))

    def _headers(self):
        """
        Build HTTP headers for API requests, including provider-aware auth.
        """
        h = {"Content-Type": "application/json"}
        auth_info = self._resolve_auth()
        if auth_info:
            auth_headers = build_auth_headers(auth_info)
            h.update(auth_headers)

        # Legacy fallback for simple/manual endpoints: if an api_key exists and no
        # auth header was added (e.g. style='none' or unknown provider), add Bearer.
        api_key = self.config.get("api_key", "").strip()
        if api_key and "Authorization" not in h and "x-api-key" not in h:
            h["Authorization"] = f"Bearer {api_key}"

        # identification
        h["HTTP-Referer"] = APP_REFERER
        h["X-Title"] = APP_TITLE
        return h

    def _resolve_auth(self):
        """Resolve auth info from config."""
        try:
            return resolve_auth_for_config(self.config)
        except AuthError as e:
            log.error(f"Auth resolution error: {e}")
            return {}

    def _get_provider(self):
        """Get the provider ID from resolved auth."""
        auth_info = self._resolve_auth()
        return auth_info.get("provider", "custom")

    def _timeout(self):
        return self.config.get("request_timeout", 120)

    def _current_host(self):
        endpoint = self._endpoint()
        urllib.parse.urlparse(endpoint)
        return get_url_hostname(endpoint)

    def _enable_local_ssl_fallback(self, err):
        """Switch a local HTTPS host to unverified mode after cert validation fails."""
        host = self._current_host()
        if not host or not _is_local_host(host) or not _is_certificate_verify_error(err):
            return False
        if host in self._ssl_fallback_hosts:
            return False
        self._ssl_fallback_hosts.add(host)
        log.error("Local HTTPS certificate verification failed for %s; retrying unverified." % host)
        self._close_connection()
        return True

    def make_api_request(self, prompt, system_prompt="", max_tokens=70):
        """Build a streaming chat completions request (legacy/simple wrapper)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.make_chat_request(messages, max_tokens=max_tokens, stream=True)

    def extract_content_from_response(self, chunk):
        """Extract text content and optional thinking from response chunk (provider-aware)."""
        return self._get_shim().parse_response_chunk(chunk)

    def make_chat_request(self, messages, max_tokens=512, tools=None, stream=False, model=None, response_format=None, *, prepend_dev_build_system_prefix: bool = True):
        """Build a chat completions request from a full messages array (provider-aware)."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 512

        # 0. Coalesce consecutive system messages
        coalesced_messages: list[Any] = []
        coalesced_any = False
        for m in messages:
            if coalesced_messages and m.get("role") == "system" and coalesced_messages[-1].get("role") == "system":
                prev_content = coalesced_messages[-1].get("content", "")
                curr_content = m.get("content", "")
                if not isinstance(prev_content, str) or not isinstance(curr_content, str):
                    err_msg = "make_chat_request: System message content is not a string."
                    log.error(err_msg)
                    raise ValueError(err_msg)
                coalesced_messages[-1]["content"] = prev_content + "\n\n" + curr_content
                coalesced_any = True
            else:
                coalesced_messages.append(dict(m) if isinstance(m, dict) else m)

        if coalesced_any:
            log.error("make_chat_request: Coalesced multiple consecutive system messages.")

        messages = coalesced_messages

        # 1. Inject date into the first system message
        today = datetime.date.today().strftime("%A, %Y-%m-%d")
        date_msg = f"Today's date is {today}."
        system_message: Any = None
        for m in messages:
            if m.get("role") == "system":
                system_message = m
                break

        if system_message:
            old_content = system_message.get("content")
            if isinstance(old_content, str):
                already_has_date_line = (
                    old_content.startswith(date_msg)
                    or old_content.startswith("Today's date is ")
                    or date_msg in old_content
                )
                if not already_has_date_line:
                    system_message["content"] = f"{date_msg}\n\n{old_content}" if old_content else date_msg
        else:
            messages.insert(0, {"role": "system", "content": date_msg})

        if prepend_dev_build_system_prefix:
            _prepend_dev_build_system_prefix_to_messages(messages)

        model_name = model or self.config.get("model", "")
        temperature = self.config.get("temperature", 0.5)

        shim = self._get_shim()
        method, path, body, headers = shim.build_chat_request(messages, max_tokens, temperature, tools, stream, model_name, response_format)

        init_logging(self.ctx)
        log.debug("=== Chat Request (provider=%s, tools=%s, stream=%s) ===" % (self._get_provider(), bool(tools), stream))
        log.debug("URL: %s" % path)
        log.debug("Messages: %s" % json.dumps(redact_sensitive_payload_for_log(messages), indent=2))

        return method, path, body, headers

    def make_image_request(self, prompt, model=None, width=1024, height=1024, steps=None, source_image=None, image_url=None):
        """Build an image generation request (provider-aware)."""
        shim = self._get_shim()
        return shim.build_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)

    def image_completion(self, prompt, model=None, width=1024, height=1024, steps=None, source_image=None, image_url=None):
        """Generate images using the configured provider. Returns list of base64 strings."""
        method, path, body, headers = self.make_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)
        endpoint = self._endpoint()
        url = endpoint + path if path.startswith("/") else path

        # log.debug...
        init_logging(self.ctx)
        log.debug("=== Image Request ===")
        log.debug("URL: %s" % url)

        res = sync_request(url, method=method, data=body, headers=headers)
        if not res:
            return []

        shim = self._get_shim()
        return shim.parse_image_responses(res)

    def transcribe_audio(self, wav_path, model=None):
        """Transcribe audio using the /v1/audio/transcriptions endpoint (fallback path).
        If the model supports native audio, use a chat request instead.
        """
        import uuid
        import os
        import base64
        from plugin.framework.config import has_native_audio

        # Determine model
        model_name = model or self.config.get("stt_model") or "whisper-1"

        # 1. Check if the STT model itself supports native audio
        if has_native_audio(self.ctx, model_name, self._endpoint()):
            log.debug("Using multimodal chat for transcription fallback (model: %s, level=logging.WARNING)" % model_name)
            try:
                with open(wav_path, "rb") as f:
                    audio_b64 = base64.b64encode(f.read()).decode("utf-8")

                messages = [{"role": "user", "content": [{"type": "text", "text": "Transcribe this audio exactly. Output ONLY the transcript. No preamble, no markers."}, {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}]}]

                # Using synchronous chat completion with model override
                return self.chat_completion_sync(messages, max_tokens=16384, model=model_name)
            except Exception as e:
                log.warning("Multimodal transcription failed: %s. Falling back to stt endpoint." % type(e).__name__)

        # 2. Standard multipart fallback (Whisper, etc.)
        boundary = "Boundary-%s" % uuid.uuid4().hex

        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/audio/transcriptions"

        # Build multipart/form-data body manually (urllib doesn't have a built-in helper)
        parts = []
        # file part
        filename = os.path.basename(wav_path)
        parts.append(("--%s" % boundary).encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="file"; filename="%s"' % filename).encode("utf-8"))
        parts.append(b"Content-Type: audio/wav")
        parts.append(b"")
        with open(wav_path, "rb") as f:
            parts.append(f.read())

        # model part
        parts.append(("--%s" % boundary).encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="model"').encode("utf-8"))
        parts.append(b"")
        parts.append(model_name.encode("utf-8"))

        # End boundary
        parts.append(("--%s--" % boundary).encode("utf-8"))
        parts.append(b"")

        # Headers: use base headers but override Content-Type
        headers = self._headers()
        headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary

        body_bytes = b"\r\n".join(parts)

        log.debug("=== STT Request ===")
        log.debug("URL: %s" % url)
        log.debug("STT Model: %s" % model_name)

        # use sync_request (blocking helper already in this file)
        res = sync_request(url, data=body_bytes, headers=headers)
        return res.get("text", "") if isinstance(res, dict) else str(res)

    def stream_completion(self, prompt, system_prompt, max_tokens, append_callback, append_thinking_callback=None, stop_checker=None, status_callback=None):
        """Stream a chat completions response via callbacks."""
        method, path, body, headers = self.make_api_request(prompt, system_prompt, max_tokens)
        self.stream_request(method, path, body, headers, append_callback, append_thinking_callback, stop_checker=stop_checker)

    def _run_streaming_loop(self, method, path, body, headers, on_content, on_thinking=None, on_delta=None, stop_checker=None, _retry=True):
        """Common low-level streaming engine."""
        init_logging(self.ctx)
        log.debug("=== Starting streaming loop (persistent, level=logging.INFO) ===")
        log.debug("Request Path: %s" % path)

        last_finish_reason = None
        conn = self._get_connection()

        try:
            self._pace_before_llm_request()
            conn.request(method, path, body=body, headers=headers)
            self._mark_llm_request_sent()
            response = conn.getresponse()

            if response.status != 200:
                err_body = response.read().decode("utf-8", errors="replace")
                log.error("Provider API Error %d: %s" % (response.status, err_body))
                # Close on error to be safe
                self._close_connection()
                raise NetworkError(_format_http_error_response(response.status, response.reason, err_body), code="HTTP_ERROR", context={"url": path, "status": response.status})

            try:
                # Use a flag to stop logical processing but keep reading to exhaust the stream
                content_finished = False
                # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                last_contents = collections.deque(maxlen=REPEATED_STREAMING_CHUNK_LIMIT)

                self._get_provider()
                # Google Gemini stream is a JSON array of objects, not SSE.
                # Actually, iterate_sse might fail if it's not 'data: ...'.
                # For now, we assume it's SSE-like or we add custom iteration.

                for payload in iterate_sse(response):
                    if payload == "[DONE]":
                        log.info("streaming_loop: [DONE] received")
                        content_finished = True
                        continue

                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        if payload and payload != "{}":
                            log.error("streaming_loop: JSON decode error in payload: %s" % payload)
                        continue

                    # Log all chunks for debugging, even after content_finished
                    # (this might contain 'usage' data)
                    if "usage" in chunk:
                        log.debug("streaming_loop: received usage: %s" % chunk["usage"])

                    if content_finished:
                        continue

                    if stop_checker and stop_checker():
                        log.debug("streaming_loop: Stop requested.")
                        last_finish_reason = "stop"
                        content_finished = True
                        # On user stop, we usually want to kill the connection
                        # because the model might keep streaming for a long time.
                        self._close_connection()
                        continue

                    # Grok/xAI sends a final chunk with empty choices + usage
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    content, finish_reason, thinking, delta = self.extract_content_from_response(chunk)

                    # LiteLLM: streaming_handler.py ~L736 "finish_reason: error, no content string given"
                    if finish_reason == "error":
                        from plugin.framework.i18n import _

                        raise NetworkError(_("Stream ended with finish_reason=error"), code="STREAM_ERROR")

                    if thinking and on_thinking:
                        on_thinking(thinking)
                    if content and on_content:
                        on_content(content)
                        # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                        last_contents.append(content)
                        if len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT and len(content) > 2 and all(c == last_contents[0] for c in last_contents):
                            from plugin.framework.i18n import _

                            raise NetworkError(_("The model is repeating the same chunk (infinite loop). Try again or use a different model."), code="INFINITE_LOOP")
                    if delta and on_delta:
                        _normalize_delta(delta)
                        on_delta(delta)

                    if finish_reason:
                        log.debug("streaming_loop: logical finish_reason=%s" % finish_reason)
                        last_finish_reason = finish_reason
            finally:
                # Ensure the entire response body is read so the connection is reusable.
                try:
                    remaining = response.read()
                    if remaining:
                        log.debug("Consumed extra %d bytes after loop" % len(remaining))
                except Exception:
                    pass
                # Honor Connection: close so we don't try to reuse when the server closed.
                conn_hdr = (response.getheader("Connection") or "").strip().lower()
                if conn_hdr == "close":
                    self._close_connection()

        except (http.client.HTTPException, socket.error, OSError) as e:
            log.error("Connection error, closing: %s" % e)
            self._close_connection()
            # If the user requested a stop, don't retry. The connection error
            # might be a side-effect of us closing the connection in stop().
            if stop_checker and stop_checker():
                log.error("Connection error during stop; exiting streaming loop")
                return "stop"
            if self._enable_local_ssl_fallback(e):
                return self._run_streaming_loop(method, path, body, headers, on_content=on_content, on_thinking=on_thinking, on_delta=on_delta, stop_checker=stop_checker, _retry=False)

            err_msg = format_error_message(e)
            if _retry:
                log.warning("Retrying streaming request once on fresh connection")
                return self._run_streaming_loop(method, path, body, headers, on_content=on_content, on_thinking=on_thinking, on_delta=on_delta, stop_checker=stop_checker, _retry=False)
            log.error("Connection retry failed: %s" % err_msg)
            raise NetworkError(err_msg, code="CONNECTION_ERROR", context={"url": path}) from e
        except NetworkError:
            self._close_connection()
            raise
        except Exception as e:
            self._close_connection()  # Reset on any other error too
            err_msg = format_error_message(e)
            log.error("ERROR in _run_streaming_loop: %s -> %s" % (type(e).__name__, err_msg))
            raise NetworkError(err_msg, context={"url": path}) from e

        return last_finish_reason

    def stream_request(self, method, path, body, headers, append_callback, append_thinking_callback=None, stop_checker=None):
        """Stream a chat response and append chunks via callbacks."""
        self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, stop_checker=stop_checker)

    def stream_chat_response(self, messages, max_tokens, append_callback, append_thinking_callback=None, stop_checker=None, *, prepend_dev_build_system_prefix: bool = True):
        """Stream a final chat response (no tools) using the messages array."""
        method, path, body, headers = self.make_chat_request(messages, max_tokens, tools=None, stream=True, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        self.stream_request(method, path, body, headers, append_callback, append_thinking_callback, stop_checker=stop_checker)

    def request_with_tools(self, messages, max_tokens=512, tools=None, append_callback=None, append_thinking_callback=None, stop_checker=None, body_override=None, model=None, stream=False, response_format=None, prepend_dev_build_system_prefix: bool = True):
        """Chat request with support for tools and streaming.

        If stream=True, uses callbacks to stream deltas & accumulates tool_calls.
        If stream=False, makes a standard blocking call.

        Returns a dict: {role, content, tool_calls, finish_reason, images, usage}
        """
        init_logging(self.ctx)
        eff_model = model or self.config.get("model", "")
        n_tool_defs = len(tools) if isinstance(tools, list) else 0
        log.debug("request_with_tools: model=%s stream=%s n_messages=%s n_tool_defs=%s", eff_model, stream, len(messages), n_tool_defs)
        method, path, body, headers = self.make_chat_request(messages, max_tokens, tools=tools, stream=stream, model=model, response_format=response_format, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        if body_override is not None:
            body = body_override.encode("utf-8") if isinstance(body_override, str) else body_override

        message_snapshot: dict[object, object] = {}
        last_finish_reason = None
        images: list[Any] = []
        usage: dict[str, Any] = {}
        content = ""
        tool_calls = None

        if stream:
            append_callback = append_callback or (lambda t: None)
            append_thinking_callback = append_thinking_callback or (lambda t: None)

            log.debug("stream_request_with_tools: building request (%d messages)..." % len(messages))
            try:
                last_finish_reason = self._run_streaming_loop(method, path, body, headers, on_content=append_callback, on_thinking=append_thinking_callback, on_delta=lambda d: accumulate_delta(message_snapshot, d), stop_checker=stop_checker)
            except NetworkError:
                raise
            except Exception as e:
                err_msg = format_error_message(e)
                log.error("stream_request_with_tools ERROR: %s -> %s" % (type(e).__name__, err_msg))
                raise NetworkError(err_msg, context={"url": path}) from e

            raw_content = message_snapshot.get("content")
            content = _normalize_message_content(raw_content)
            tool_calls = message_snapshot.get("tool_calls")
            usage = cast("dict[str, Any]", message_snapshot.get("usage", {}))
        else:
            # Sync path
            result = None
            for attempt in (0, 1):
                try:
                    conn = self._get_connection()
                    self._pace_before_llm_request()
                    conn.request(method, path, body=body, headers=headers)
                    self._mark_llm_request_sent()
                    response = conn.getresponse()
                    if response.status != 200:
                        err_body = response.read().decode("utf-8", errors="replace")
                        log.error("Provider API Error %d: %s" % (response.status, err_body))
                        try:
                            redacted_msgs = redact_sensitive_payload_for_log(messages)
                            log.error("request_with_tools outgoing messages (redacted): %s", json.dumps(redacted_msgs, indent=2, ensure_ascii=False))
                        except Exception as log_exc:
                            log.warning("Could not log redacted outgoing messages: %s", log_exc)
                        self._close_connection()
                        raise NetworkError(_format_http_error_response(response.status, response.reason, err_body), code="HTTP_ERROR", context={"url": path, "status": response.status})
                    from plugin.framework.errors import safe_json_loads

                    result = safe_json_loads(response.read().decode("utf-8"))
                    break
                except (http.client.HTTPException, socket.error, OSError) as e:
                    log.error("Connection error, closing: %s" % e)
                    self._close_connection()
                    if self._enable_local_ssl_fallback(e):
                        continue
                    if attempt == 0:
                        log.warning("Retrying request_with_tools once on fresh connection")
                        continue
                    log.error("Connection retry failed: %s" % format_error_message(e))
                    raise NetworkError(format_error_message(e), code="CONNECTION_ERROR", context={"url": path}) from e
                except NetworkError:
                    raise
                except Exception as e:
                    err_msg = format_error_message(e)
                    log.error("request_with_tools ERROR: %s -> %s" % (type(e).__name__, err_msg))
                    raise NetworkError(err_msg, context={"url": path}) from e

            log.debug("=== Sync response: %s" % json.dumps(result, indent=2))

            if result is None:
                result = {}

            # Use unified extraction for shims/native providers
            provider = self._get_provider()
            if provider in ("anthropic", "google"):
                content, last_finish_reason, _, message = self.extract_content_from_response(result)
                tool_calls = message.get("tool_calls")
                usage = message.get("usage") or result.get("usage", {})
                images = message.get("images") or []
            else:
                # OpenAI / local default path
                choice = result.get("choices", [{}])[0] if result.get("choices") else {}
                if choice is None:
                    choice = {}
                message = choice.get("message") or result.get("message") or {}
                _normalize_delta(message)
                last_finish_reason = choice.get("finish_reason") or result.get("done_reason")

                raw_content = message.get("content")
                content = _normalize_message_content(raw_content)
                images = message.get("images") or []
                tool_calls = message.get("tool_calls")
                usage = result.get("usage", {})

        # Shared post-processing
        if last_finish_reason == "stop" and tool_calls:
            last_finish_reason = "tool_calls"

        if content:
            cleaned = strip_leaked_chat_template_control_tokens(content)
            if cleaned != content:
                log.info("Stripped leaked <|...|> chat-template tokens from assistant content (model=%s, original_len=%d, cleaned_len=%d)", eff_model, len(content), len(cleaned))
                log.debug("Stripped leaked chat-template control tokens from model content. original=%r cleaned=%r", content, cleaned)
                content = cleaned

        if not tool_calls and content:
            from plugin.contrib.tool_call_parsers import get_parser_for_model

            parser = get_parser_for_model(eff_model)
            if parser:
                p_content, p_tool_calls = parser.parse(content)
                if p_tool_calls:
                    tool_calls = p_tool_calls
                    content = p_content or ""
                    if last_finish_reason != "tool_calls":
                        last_finish_reason = "tool_calls"

        return {"role": "assistant", "content": content, "tool_calls": tool_calls, "finish_reason": last_finish_reason, "images": images, "usage": usage}

    def stream_request_with_tools(self, *args, **kwargs):
        """Streaming chat request with tools. Wrapper around request_with_tools."""
        kwargs["stream"] = True
        return self.request_with_tools(*args, **kwargs)

    def chat_completion_sync(self, messages, max_tokens=512, model=None, response_format=None, *, prepend_dev_build_system_prefix: bool = True):
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.
        """
        result = self.request_with_tools(messages, max_tokens=max_tokens, tools=None, model=model, response_format=response_format, prepend_dev_build_system_prefix=prepend_dev_build_system_prefix)
        return result.get("content") or ""
