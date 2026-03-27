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
import json
import urllib.parse
import http.client
import socket
import datetime

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20

# accumulate_delta is required for tool-calling: it merges streaming deltas into message_snapshot so full tool_calls (with function.arguments) are available.
from plugin.framework.streaming_deltas import accumulate_delta
from plugin.framework.constants import APP_REFERER, APP_TITLE

from plugin.framework.logging import init_logging
from plugin.framework.auth import resolve_auth_for_config, build_auth_headers, AuthError
from plugin.framework.errors import NetworkError, safe_json_loads
from plugin.framework.utils import get_url_hostname, get_url_path_and_query

from plugin.modules.http.errors import format_error_message, _format_http_error_response
from plugin.modules.http.ssl_helpers import (
    get_unverified_ssl_context,
    get_verified_ssl_context,
    _is_certificate_verify_error,
    _is_local_host,
)
from plugin.modules.http.stream_normalizer import (
    iterate_sse,
    _extract_thinking_from_delta,
    _normalize_message_content,
    _normalize_delta,
)
from plugin.modules.http.requests import sync_request

log = logging.getLogger(__name__)


class LlmClient:
    """LLM API client. Takes config dict from get_api_config(ctx) and UNO ctx."""

    def __init__(self, config, ctx):
        self.config = config
        self.ctx = ctx
        self._persistent_conn = None
        self._conn_key = None  # (scheme, host, port)
        self._ssl_fallback_hosts = set()

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
            except:
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
        return "/api" if self.config.get("is_openwebui") else "/v1"

    def _headers(self):
        """
        Build HTTP headers for API requests, including provider-aware auth.

        Auth resolution is delegated to plugin.framework.auth so different
        endpoints (OpenRouter, Together, local, etc.) can attach API keys
        correctly based on the configured endpoint. On any auth resolution
        error we fall back to the legacy Bearer logic so misconfiguration
        degrades gracefully.
        """
        h = {"Content-Type": "application/json"}

        try:
            auth_info = resolve_auth_for_config(self.config)
            auth_headers = build_auth_headers(auth_info)
            h.update(auth_headers)
        except AuthError as e:
            # Fall back to the previous behavior: simple Bearer header from config.
            log.error(f"Auth resolution error ({e.provider or 'unknown'}, level=logging.ERROR): {e}")
            api_key = self.config.get("api_key", "")
            if api_key:
                h["Authorization"] = "Bearer %s" % api_key

        # Backwards-compatible behavior for simple/local endpoints:
        # if the user configured an api_key but provider-specific auth did not
        # attach an Authorization header, add the legacy Bearer header.
        api_key = self.config.get("api_key", "")
        if api_key and "Authorization" not in h:
            h["Authorization"] = "Bearer %s" % api_key

        h["HTTP-Referer"] = APP_REFERER
        h["X-Title"] = APP_TITLE

        return h

    def _timeout(self):
        return self.config.get("request_timeout", 120)

    def _current_host(self):
        endpoint = self._endpoint()
        parsed = urllib.parse.urlparse(endpoint)
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
        """Build a streaming chat completions request (always chat, no completions path)."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 70

        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/chat/completions"
        model = self.config.get("model", "")
        temperature = self.config.get("temperature", 0.5)

        init_logging(self.ctx)
        log.debug("=== API Request Debug ===")
        log.debug("Endpoint: %s" % endpoint)
        log.debug("Model: %s" % model)
        log.debug("Max Tokens: %s" % max_tokens)

        messages = []
        if system_prompt:
            today = datetime.date.today().strftime("%A, %Y-%m-%d")
            full_system_prompt = f"Today's date is {today}.\n\n{system_prompt}"
            messages.append({"role": "system", "content": full_system_prompt})
        messages.append({"role": "user", "content": prompt})
        data = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stream": True,
        }
        if model:
            data["model"] = model

        json_data = json.dumps(data).encode("utf-8")
        path = get_url_path_and_query(url)

        log.debug("Request data: %s" % json.dumps(data, indent=2))
        return "POST", path, json_data, self._headers()

    def extract_content_from_response(self, chunk):
        """Extract text content and optional thinking from chat completions response chunk."""
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

    def make_chat_request(self, messages, max_tokens=512, tools=None, stream=False, model=None):
        """Build a chat completions request from a full messages array."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 512

        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/chat/completions"
        model_name = model or self.config.get("model", "")
        temperature = self.config.get("temperature", 0.5)

        data = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stream": stream,
        }

        # Inject date into the first system message if present, or add one.
        # This is idempotent: if the first system message already starts with
        # the date line, we do not prepend it again.
        today = datetime.date.today().strftime("%A, %Y-%m-%d")
        date_msg = f"Today's date is {today}."

        system_msg = None
        for m in messages:
            if m.get("role") == "system":
                system_msg = m
                break

        if system_msg:
            old_content = system_msg.get("content")
            # Some models/tools can provide structured (non-string) system content
            # (e.g. multimodal content parts). In that case, skip date injection
            # to avoid calling string methods on non-strings.
            if not isinstance(old_content, str):
                old_content = None
            if old_content is not None:
                if not (
                    old_content.startswith(date_msg)
                    or old_content.startswith("Today's date is ")
                ):
                    if old_content:
                        system_msg["content"] = f"{date_msg}\n\n{old_content}"
                    else:
                        system_msg["content"] = date_msg
        else:
            messages.insert(0, {"role": "system", "content": date_msg})

        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            data["parallel_tool_calls"] = False

        json_data = json.dumps(data).encode("utf-8")
        init_logging(self.ctx)
        log.debug(
            "=== Chat Request (tools=%s, stream=%s) ===" % (bool(tools), stream)
        )
        log.debug("URL: %s" % url)
        log.debug("Messages: %s" % json.dumps(messages, indent=2))
        
        path = get_url_path_and_query(url)
            
        return "POST", path, json_data, self._headers()
            
    def make_image_request(self, prompt, model=None, width=1024, height=1024, steps=None, source_image=None, image_url=None):
        """Build an image generation request (OpenAI-compatible /images/generations).
        When source_image (base64 str) or image_url is provided, include image_url in the body for img2img (e.g. Together, FLUX)."""
        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/images/generations"
        model_name = model or self.config.get("model", "")
        
        data = {
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "url",
        }
        if model_name:
            data["model"] = model_name
        if steps:
            data["steps"] = steps
        if image_url:
            data["image_url"] = image_url
        elif source_image:
            data["image_url"] = "data:image/png;base64," + source_image

        json_data = json.dumps(data).encode("utf-8")
        init_logging(self.ctx)
        log.debug("=== Image Request ===")
        log.debug("URL: %s" % url)
        log.debug("Data: %s" % json.dumps(data, indent=2))
        
        path = get_url_path_and_query(url)
            
        return "POST", path, json_data, self._headers()

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
                
                messages = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this audio exactly. Output ONLY the transcript. No preamble, no markers."},
                        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}
                    ]
                }]
                
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
        parts.append(b'Content-Type: audio/wav')
        parts.append(b'')
        with open(wav_path, "rb") as f:
            parts.append(f.read())
            
        # model part
        parts.append(("--%s" % boundary).encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="model"').encode("utf-8"))
        parts.append(b'')
        parts.append(model_name.encode("utf-8"))
        
        # End boundary
        parts.append(("--%s--" % boundary).encode("utf-8"))
        parts.append(b'')
        
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

    def stream_completion(
        self,
        prompt,
        system_prompt,
        max_tokens,
        append_callback,
        append_thinking_callback=None,
        stop_checker=None,
        status_callback=None,
    ):
        """Stream a chat completions response via callbacks."""
        method, path, body, headers = self.make_api_request(
            prompt, system_prompt, max_tokens
        )
        self.stream_request(
            method, path, body, headers,
            append_callback,
            append_thinking_callback,
            stop_checker=stop_checker,
        )

    def _run_streaming_loop(
        self,
        method,
        path,
        body,
        headers,
        on_content,
        on_thinking=None,
        on_delta=None,
        stop_checker=None,
        _retry=True,
    ):
        """Common low-level streaming engine."""
        init_logging(self.ctx)
        log.debug("=== Starting streaming loop (persistent, level=logging.INFO) ===")
        log.debug("Request Path: %s" % path)

        last_finish_reason = None
        conn = self._get_connection()
        
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            
            if response.status != 200:
                err_body = response.read().decode("utf-8", errors="replace")
                log.error("Provider API Error %d: %s" % (response.status, err_body))
                # Close on error to be safe
                self._close_connection()
                raise NetworkError(
                    _format_http_error_response(response.status, response.reason, err_body),
                    code="HTTP_ERROR",
                    context={"url": path, "status": response.status}
                )

            try:
                # Use a flag to stop logical processing but keep reading to exhaust the stream
                content_finished = False
                # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                last_contents = collections.deque(maxlen=REPEATED_STREAMING_CHUNK_LIMIT)
                for payload in iterate_sse(response):
                    
                    if payload == "[DONE]":
                        log.info("streaming_loop: [DONE] received")
                        content_finished = True
                        continue
                    
                    chunk = safe_json_loads(payload, default=None)
                    if chunk is None:
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

                    content, finish_reason, thinking, delta = (
                        self.extract_content_from_response(chunk)
                    )

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
                        if (len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT
                                and len(content) > 2
                                and all(c == last_contents[0] for c in last_contents)):
                            from plugin.framework.i18n import _
                            raise NetworkError(
                                _("The model is repeating the same chunk (infinite loop). Try again or use a different model."),
                                code="INFINITE_LOOP"
                            )
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
                except:
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
                return self._run_streaming_loop(
                    method, path, body, headers,
                    on_content=on_content,
                    on_thinking=on_thinking,
                    on_delta=on_delta,
                    stop_checker=stop_checker,
                    _retry=False,
                )
            
            err_msg = format_error_message(e)
            if _retry:
                log.warning("Retrying streaming request once on fresh connection")
                return self._run_streaming_loop(
                    method, path, body, headers,
                    on_content=on_content,
                    on_thinking=on_thinking,
                    on_delta=on_delta,
                    stop_checker=stop_checker,
                    _retry=False,
                )
            log.error("Connection retry failed: %s" % err_msg)
            raise NetworkError(err_msg, code="CONNECTION_ERROR", context={"url": path}) from e
        except NetworkError:
            self._close_connection()
            raise
        except Exception as e:
            self._close_connection() # Reset on any other error too
            err_msg = format_error_message(e)
            log.error("ERROR in _run_streaming_loop: %s -> %s" % (type(e).__name__, err_msg))
            raise NetworkError(err_msg, context={"url": path}) from e

        return last_finish_reason

    def stream_request(
        self,
        method,
        path,
        body,
        headers,
        append_callback,
        append_thinking_callback=None,
        stop_checker=None,
    ):
        """Stream a chat response and append chunks via callbacks."""
        self._run_streaming_loop(
            method,
            path,
            body,
            headers,
            on_content=append_callback,
            on_thinking=append_thinking_callback,
            stop_checker=stop_checker,
        )

    def stream_chat_response(
        self,
        messages,
        max_tokens,
        append_callback,
        append_thinking_callback=None,
        stop_checker=None,
    ):
        """Stream a final chat response (no tools) using the messages array."""
        method, path, body, headers = self.make_chat_request(
            messages, max_tokens, tools=None, stream=True
        )
        self.stream_request(
            method, path, body, headers,
            append_callback,
            append_thinking_callback,
            stop_checker=stop_checker,
        )

    def request_with_tools(
        self,
        messages,
        max_tokens=512,
        tools=None,
        append_callback=None,
        append_thinking_callback=None,
        stop_checker=None,
        body_override=None,
        model=None,
        stream=False,
    ):
        """Chat request with support for tools and streaming.
        
        If stream=True, uses callbacks to stream deltas & accumulates tool_calls.
        If stream=False, makes a standard blocking call.
        
        Returns a dict: {role, content, tool_calls, finish_reason, images, usage}
        """
        init_logging(self.ctx)
        method, path, body, headers = self.make_chat_request(
            messages, max_tokens, tools=tools, stream=stream, model=model
        )
        if body_override is not None:
            body = body_override.encode("utf-8") if isinstance(body_override, str) else body_override

        message_snapshot = {}
        last_finish_reason = None
        images = []
        usage = {}
        content = ""
        tool_calls = None

        if stream:
            append_callback = append_callback or (lambda t: None)
            append_thinking_callback = append_thinking_callback or (lambda t: None)

            log.debug("stream_request_with_tools: building request (%d messages)..." % len(messages))
            try:
                last_finish_reason = self._run_streaming_loop(
                    method,
                    path,
                    body,
                    headers,
                    on_content=append_callback,
                    on_thinking=append_thinking_callback,
                    on_delta=lambda d: accumulate_delta(message_snapshot, d),
                    stop_checker=stop_checker,
                )
            except NetworkError:
                raise
            except Exception as e:
                err_msg = format_error_message(e)
                log.error("stream_request_with_tools ERROR: %s -> %s" % (type(e).__name__, err_msg))
                raise NetworkError(err_msg, context={"url": path}) from e

            raw_content = message_snapshot.get("content")
            content = _normalize_message_content(raw_content)
            tool_calls = message_snapshot.get("tool_calls")
            usage = message_snapshot.get("usage", {})
        else:
            # Sync path
            result = None
            for attempt in (0, 1):
                try:
                    conn = self._get_connection()
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()
                    if response.status != 200:
                        err_body = response.read().decode("utf-8", errors="replace")
                        log.error("Provider API Error %d: %s" % (response.status, err_body))
                        self._close_connection()
                        raise NetworkError(
                            _format_http_error_response(response.status, response.reason, err_body),
                            code="HTTP_ERROR",
                            context={"url": path, "status": response.status}
                        )
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

            choice = result.get("choices", [{}])[0] if result.get("choices") else {}
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

        if not tool_calls and content:
            from plugin.contrib.tool_call_parsers import get_parser_for_model
            parser = get_parser_for_model(model or self.config.get("model", ""))
            if parser:
                p_content, p_tool_calls = parser.parse(content)
                if p_tool_calls:
                    tool_calls = p_tool_calls
                    content = p_content
                    if last_finish_reason != "tool_calls":
                        last_finish_reason = "tool_calls"

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": last_finish_reason,
            "images": images,
            "usage": usage,
        }

    def stream_request_with_tools(self, *args, **kwargs):
        """Streaming chat request with tools. Wrapper around request_with_tools."""
        kwargs["stream"] = True
        return self.request_with_tools(*args, **kwargs)


    def chat_completion_sync(self, messages, max_tokens=512, model=None):
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.
        """
        result = self.request_with_tools(
            messages, max_tokens=max_tokens, tools=None, model=model
        )
        return result.get("content") or ""