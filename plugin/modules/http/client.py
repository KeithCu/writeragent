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
import ssl
import urllib.request
import urllib.parse
import http.client
import socket
import ipaddress
import datetime

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20

# accumulate_delta is required for tool-calling: it merges streaming deltas into message_snapshot so full tool_calls (with function.arguments) are available.
from plugin.framework.streaming_deltas import accumulate_delta
from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT

from plugin.framework.logging import init_logging
from plugin.framework.auth import resolve_auth_for_config, build_auth_headers, AuthError
from plugin.framework.errors import NetworkError, AgentParsingError

log = logging.getLogger(__name__)


def format_error_message(e):
    """Map common exceptions to user-friendly advice."""
    import urllib.error

    msg = str(e)
    if isinstance(e, ssl.SSLError):
        return "TLS/SSL Error: %s" % msg
    if isinstance(e, (urllib.error.HTTPError, http.client.HTTPResponse)):
        code = e.code if hasattr(e, "code") else e.status
        reason = e.reason if hasattr(e, "reason") else ""
        if code == 401:
            return "Invalid API Key. Please check your settings."
        if code == 403:
            return "API access Forbidden. Your key may lack permissions for this model."
        if code == 404:
            return "Endpoint not found (404). Check your URL and Model name."
        if code >= 500:
            return "Server error (%d). The AI provider is having issues." % code
        return "HTTP Error %d: %s" % (code, reason)

    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return "Request Timed Out. Try increasing 'Request Timeout' in Settings."

    if isinstance(e, (urllib.error.URLError, socket.error)):
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        if "Connection refused" in reason or "111" in reason:
            return "Connection Refused. Is your local AI server (Ollama/LM Studio) running?"
        if "getaddrinfo failed" in reason:
            return "DNS Error. Could not resolve the endpoint URL."
        return "Connection Error: %s" % reason

    if "finish_reason=error" in msg:
        return "The AI provider reported an error. Try again."

    return msg


def _format_http_error_response(status, reason, err_body):
    """Build error message including response body for display in chat/UI."""
    base = "HTTP Error %d: %s" % (status, reason)
    if not err_body or not err_body.strip():
        return base
    try:
        data = json.loads(err_body)
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or err.get("error") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            return base + ". " + detail
    except (json.JSONDecodeError, TypeError):
        pass
    snippet = err_body.strip().replace("\n", " ")[:400]
    return base + ". " + snippet


def format_error_for_display(e):
    """Return user-friendly error string for display in cells or dialogs."""
    from plugin.framework.errors import format_error_payload
    payload = format_error_payload(e)
    return "Error: %s" % payload.get("message", format_error_message(e))


def is_audio_unsupported_error(e):
    """Try to determine if the error indicates that audio/modality is unsupported by the model."""
    msg = str(e).lower()
    
    # Common error strings across providers
    if "unsupported content type" in msg: return True
    if "unsupported modality" in msg: return True
    if "audio" in msg and ("not supported" in msg or "unsupported" in msg): return True
    if "modality" in msg and "not supported" in msg: return True
    
    # Specific API error bodies (passed via _format_http_error_response)
    if "model" in msg and "cannot process" in msg and "audio" in msg: return True
    if "no endpoints found that support input audio" in msg: return True
    if "gpt-4" in msg and "audio" in msg: # Some legacy GPT-4 might not have it
        if "not support" in msg: return True
        
    return False


def get_unverified_ssl_context():
    """Create an SSL context that doesn't verify certificates. Shared by API and aihordeclient."""
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def get_verified_ssl_context():
    """Create a default verifying SSL context."""
    return ssl.create_default_context()


def _is_certificate_verify_error(e):
    """Return True when an exception points to certificate validation failure."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    msg = ("%s %s" % (e, reason or "")).lower()
    for marker in (
        "certificate_verify_failed",
        "certificate verify failed",
        "self-signed certificate",
        "self signed certificate",
        "unable to get local issuer certificate",
        "hostname mismatch",
    ):
        if marker in msg:
            return True
    return False


def _is_local_host(host):
    """Heuristic for localhost / LAN hosts where self-signed TLS is common."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "host.docker.internal"):
        return True
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass
    # Single-label hostnames are usually local network names.
    return "." not in host


def sync_request(url, data=None, headers=None, timeout=10, parse_json=True):
    """
    Blocking HTTP GET or POST. Shared by aihordeclient and other code.
    url: str or urllib.request.Request. If Request, headers/data come from it.
    data: optional bytes for POST. headers: optional dict (used only if url is str).
    Returns response data: decoded JSON if parse_json else raw bytes. Raises on error.
    """
    import urllib.error
    if headers is None:
        headers = {}
    
    # Add default headers to avoid being blocked and provide application identity
    has_ua = any(k.lower() == "user-agent" for k in headers.keys())
    if not has_ua:
        headers["User-Agent"] = USER_AGENT
    
    if "HTTP-Referer" not in headers:
        headers["HTTP-Referer"] = APP_REFERER
    if "X-Title" not in headers:
        headers["X-Title"] = APP_TITLE

    if isinstance(url, str):
        req = urllib.request.Request(url, data=data, headers=headers)
    else:
        req = url
    
    # Debug: log which headers we are actually sending (keys only)
    try:
        header_keys = list(req.headers.keys()) if hasattr(req, "headers") else []
        if not header_keys and hasattr(req, "get_full_url"):
            # If it's a urllib Request object, headers might be in .headers
            pass 
        log.debug(f"Request to {getattr(req, 'full_url', url)} with header keys: {header_keys}")
    except Exception:
        pass

    full_url = getattr(req, "full_url", url)
    parsed = urllib.parse.urlparse(str(full_url))
    host = parsed.hostname or ""
    is_local_https = parsed.scheme.lower() == "https" and _is_local_host(host)
    def _read_with_context(context):
        log.debug(f"About to open URL: {getattr(req, 'full_url', url)}")
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            log.debug(f"URL opened, status={resp.getcode()}. Heading to read...")
            raw = resp.read()
            log.debug(f"Read {len(raw)} bytes")
            if parse_json:
                return json.loads(raw.decode("utf-8"))
            return raw

    ctx = get_verified_ssl_context() if is_local_https else get_unverified_ssl_context()
    try:
        return _read_with_context(ctx)
    except urllib.error.HTTPError as e:
        status = e.code
        reason = e.reason
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        
        msg = _format_http_error_response(status, reason, err_body)
        log.error(f"HTTP Error: {msg}")
        raise NetworkError(msg, code="HTTP_ERROR", context={"url": url, "status": status}) from e
    except NetworkError:
        raise
    except Exception as e:
        if is_local_https and _is_certificate_verify_error(e):
            log.error("Local HTTPS certificate verification failed for %s; retrying unverified." % host)
            try:
                return _read_with_context(get_unverified_ssl_context())
            except urllib.error.HTTPError as retry_http_e:
                status = retry_http_e.code
                reason = retry_http_e.reason
                try:
                    err_body = retry_http_e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                msg = _format_http_error_response(status, reason, err_body)
                log.error(f"HTTP Error: {msg}")
                raise NetworkError(msg, code="HTTP_ERROR", context={"url": url, "status": status}) from retry_http_e
            except Exception as retry_e:
                log.error(f"Request failed: {format_error_message(retry_e)}")
                raise NetworkError(format_error_message(retry_e), context={"url": url}) from retry_e
        log.error(f"Request failed: {format_error_message(e)}")
        raise NetworkError(format_error_message(e), context={"url": url}) from e


def iterate_sse(stream):
    """
    Iterate over SSE (Server-Sent Events) data payloads from a stream of lines (bytes).
    Yields the payload string (everything after 'data:').
    """
    for line in stream:
        line_str = line.strip()
        if not line_str or line_str.startswith(b":"):
            continue

        if not line_str.startswith(b"data:"):
            continue
        
        # Payload is everything after the first ":"
        idx = line_str.find(b":") + 1
        payload = line_str[idx:].decode("utf-8").strip()
        yield payload


def _extract_thinking_from_delta(chunk_delta):
    """Extract reasoning/thinking text from a stream delta for display in UI."""
    # Try direct fields first
    for field in ["reasoning_content", "thought", "thinking"]:
        thinking = chunk_delta.get(field)
        if isinstance(thinking, str) and thinking:
            return thinking
    
    # Try reasoning_details array
    details = chunk_delta.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("reasoning.text", "thought", "reasoning"):
                    parts.append(item.get("text") or "")
                elif item_type == "reasoning.summary":
                    parts.append(item.get("summary") or "")
        if parts:
            return "".join(parts)
    
    # Try choices[0].delta if not found at top level
    choices = chunk_delta.get("choices")
    if choices and isinstance(choices, list) and len(choices) > 0:
        delta = choices[0].get("delta", {})
        if delta:
            return _extract_thinking_from_delta(delta)

    return ""


def _normalize_message_content(raw):
    """Return a single string from API message content (string or list of parts)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text") or "")
                elif "text" in item:
                    parts.append(item.get("text") or "")
        return "".join(parts) if parts else None
    return str(raw)


def _normalize_delta(delta):
    """Normalize delta for Mistral/Azure compat before accumulate_delta.
    LiteLLM: streaming_handler.py ~L847 (role), ~L853 (type), ~L820 (arguments).
    """
    if not isinstance(delta, dict):
        return
    # LiteLLM: streaming_handler.py ~L847 "mistral's api returns role as None"
    if "role" in delta and delta["role"] is None:
        delta["role"] = "assistant"
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        # LiteLLM: streaming_handler.py ~L853 "mistral's api returns type: None"
        if tc.get("type") is None:
            tc["type"] = "function"
        fn = tc.get("function")
        # LiteLLM: streaming_handler.py ~L820 "## AZURE - check if arguments is not None"
        if isinstance(fn, dict) and fn.get("arguments") is None:
            fn["arguments"] = ""


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
        host = parsed.hostname
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
        return parsed.hostname or ""

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
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query

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
        
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
            
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
        
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
            
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
                log.error("API Error %d: %s" % (response.status, err_body))
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
                    
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        log.error("streaming_loop: JSON decode error in payload: %s" % payload)
                        continue
                    if chunk is None:
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
                        raise NetworkError("Stream ended with finish_reason=error", code="STREAM_ERROR")

                    if thinking and on_thinking:
                        on_thinking(thinking)
                    if content and on_content:
                        on_content(content)
                        # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                        last_contents.append(content)
                        if (len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT
                                and len(content) > 2
                                and all(c == last_contents[0] for c in last_contents)):
                            raise NetworkError(
                                "The model is repeating the same chunk (infinite loop). "
                                "Try again or use a different model.",
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

    def request_with_tools(self, messages, max_tokens=512, tools=None, body_override=None, model=None):
        """Non-streaming chat request. Returns parsed response dict. body_override: optional str/bytes to use as request body (e.g. for modalities)."""
        method, path, body, headers = self.make_chat_request(
            messages, max_tokens, tools=tools, stream=False, model=model
        )
        if body_override is not None:
            body = body_override.encode("utf-8") if isinstance(body_override, str) else body_override

        result = None
        for attempt in (0, 1):
            try:
                conn = self._get_connection()
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                if response.status != 200:
                    err_body = response.read().decode("utf-8", errors="replace")
                    log.error("API Error %d: %s" % (response.status, err_body))
                    self._close_connection()
                    raise NetworkError(
                        _format_http_error_response(response.status, response.reason, err_body),
                        code="HTTP_ERROR",
                        context={"url": path, "status": response.status}
                    )
                result = json.loads(response.read().decode("utf-8"))
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

        log.debug("=== Tool response: %s" % json.dumps(result, indent=2))

        choice = result.get("choices", [{}])[0] if result.get("choices") else {}
        message = choice.get("message") or result.get("message") or {}
        # LiteLLM: same Mistral/Azure compat as _normalize_delta (streaming_handler.py ~L820, ~L847, ~L853)
        _normalize_delta(message)
        finish_reason = choice.get("finish_reason") or result.get("done_reason")

        raw_content = message.get("content")
        content = _normalize_message_content(raw_content)
        images = message.get("images") or []
        tool_calls = message.get("tool_calls")

        if not tool_calls and content:
            from plugin.contrib.tool_call_parsers import get_parser_for_model
            parser = get_parser_for_model(model or self.config.get("model", ""))
            if parser:
                p_content, p_tool_calls = parser.parse(content)
                if p_tool_calls:
                    tool_calls = p_tool_calls
                    content = p_content

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "images": images,
            "usage": result.get("usage", {}),
        }

    def stream_request_with_tools(
        self,
        messages,
        max_tokens=512,
        tools=None,
        append_callback=None,
        append_thinking_callback=None,
        stop_checker=None,
    ):
        """Streaming chat request with tools. Returns same shape as request_with_tools."""
        init_logging(self.ctx)
        log.debug("stream_request_with_tools: building request (%d messages, level=logging.DEBUG)..." % len(messages))
        method, path, body, headers = self.make_chat_request(
            messages, max_tokens, tools=tools, stream=True
        )

        message_snapshot = {}
        last_finish_reason = None

        append_callback = append_callback or (lambda t: None)
        append_thinking_callback = append_thinking_callback or (lambda t: None)

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

        # LiteLLM: streaming_handler.py ~L970 finish_reason_handler() "## if tool use"
        if last_finish_reason == "stop" and message_snapshot.get("tool_calls"):
            last_finish_reason = "tool_calls"

        raw_content = message_snapshot.get("content")
        content = _normalize_message_content(raw_content)
        tool_calls = message_snapshot.get("tool_calls")

        if not tool_calls and content:
            from plugin.contrib.tool_call_parsers import get_parser_for_model
            parser = get_parser_for_model(self.config.get("model", ""))
            if parser:
                p_content, p_tool_calls = parser.parse(content)
                if p_tool_calls:
                    tool_calls = p_tool_calls
                    content = p_content
                    # If we parsed tool calls, finish reason should be 'tool_calls'
                    if last_finish_reason != "tool_calls":
                        last_finish_reason = "tool_calls"

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": last_finish_reason,
            "usage": message_snapshot.get("usage", {}),
        }

    def chat_completion_sync(self, messages, max_tokens=512, model=None):
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.
        """
        result = self.request_with_tools(
            messages, max_tokens=max_tokens, tools=None, model=model
        )
        return result.get("content") or ""