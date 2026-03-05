"""
LLM API client for LocalWriter.
Takes a config dict (from plugin.modules.core.services.config.get_api_config) and UNO ctx.
"""
import collections
import json
import ssl
import urllib.request
import urllib.parse
import http.client
import socket

# LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
REPEATED_STREAMING_CHUNK_LIMIT = 20
from collections import deque

# accumulate_delta is required for tool-calling: it merges streaming deltas into message_snapshot so full tool_calls (with function.arguments) are available.
from plugin.modules.core.streaming_deltas import accumulate_delta
from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT

from plugin.framework.logging import debug_log, update_activity_state, init_logging


def format_error_message(e):
    """Map common exceptions to user-friendly advice."""
    import urllib.error

    msg = str(e)
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

    if isinstance(e, (urllib.error.URLError, socket.error)):
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        if "Connection refused" in reason or "111" in reason:
            return "Connection Refused. Is your local AI server (Ollama/LM Studio) running?"
        if "getaddrinfo failed" in reason:
            return "DNS Error. Could not resolve the endpoint URL."
        return "Connection Error: %s" % reason

    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return "Request Timed Out. Try increasing 'Request Timeout' in Settings."

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
    return "Error: %s" % format_error_message(e)


def get_unverified_ssl_context():
    """Create an SSL context that doesn't verify certificates. Shared by API and aihordeclient."""
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


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
        debug_log(f"Request to {getattr(req, 'full_url', url)} with header keys: {header_keys}", context="API")
    except Exception:
        pass

    ctx = get_unverified_ssl_context()
    try:
        debug_log(f"About to open URL: {getattr(req, 'full_url', url)}", context="API")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            debug_log(f"URL opened, status={resp.getcode()}. Heading to read...", context="API")
            raw = resp.read()
            debug_log(f"Read {len(raw)} bytes", context="API")
            if parse_json:
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as e:
        status = e.code
        reason = e.reason
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        
        msg = _format_http_error_response(status, reason, err_body)
        debug_log(f"HTTP Error: {msg}", context="API")
        raise Exception(msg) from e
    except Exception as e:
        debug_log(f"Request failed: {format_error_message(e)}", context="API")
        raise


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


def _is_openai_compatible(config):
    endpoint = config.get("endpoint", "")
    return config.get("openai_compatibility", True) or (
        "api.openai.com" in endpoint.lower()
    )


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
            
        new_key = (scheme, host, port)
        
        if self._persistent_conn:
            if self._conn_key != new_key:
                debug_log("Closing old connection to %s, opening new to %s" % (self._conn_key, new_key), context="API")
                self._persistent_conn.close()
                self._persistent_conn = None
            else:
                return self._persistent_conn

        debug_log("Opening new connection to %s://%s:%s" % (scheme, host, port), context="API")
        self._conn_key = new_key
        timeout = self._timeout()
        
        if scheme == "https":
            ssl_context = get_unverified_ssl_context()
            self._persistent_conn = http.client.HTTPSConnection(host, port, context=ssl_context, timeout=timeout)
        else:
            self._persistent_conn = http.client.HTTPConnection(host, port, timeout=timeout)
            
        return self._persistent_conn

    def _close_connection(self):
        if self._persistent_conn:
            try:
                self._persistent_conn.close()
            except:
                pass
            self._persistent_conn = None
            self._conn_key = None

    def _endpoint(self):
        return self.config.get("endpoint", "http://127.0.0.1:5000")

    def _api_path(self):
        return "/api" if self.config.get("is_openwebui") else "/v1"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        api_key = self.config.get("api_key", "")
        if api_key:
            h["Authorization"] = "Bearer %s" % api_key
        
        h["HTTP-Referer"] = APP_REFERER
        h["X-Title"] = APP_TITLE

        return h

    def _timeout(self):
        return self.config.get("request_timeout", 120)

    def make_api_request(self, prompt, system_prompt="", max_tokens=70, api_type=None):
        """Build a streaming completion/chat request."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 70

        endpoint = self._endpoint()
        api_path = self._api_path()
        if api_type is None:
            api_type = self.config.get("api_type", "chat")
        api_type = "chat" if api_type == "chat" else "completions"
        model = self.config.get("model", "")
        temperature = self.config.get("temperature", 0.5)
        seed_val = self.config.get("seed", "")

        init_logging(self.ctx)
        debug_log("=== API Request Debug ===", context="API")
        debug_log("Endpoint: %s" % endpoint, context="API")
        debug_log("API Type: %s" % api_type, context="API")
        debug_log("Model: %s" % model, context="API")
        debug_log("Max Tokens: %s" % max_tokens, context="API")

        if api_type == "chat":
            url = endpoint + api_path + "/chat/completions"
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            data = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.9,
                "stream": True,
            }
        else:
            url = endpoint + api_path + "/completions"
            full_prompt = prompt
            if system_prompt:
                full_prompt = (
                    "SYSTEM PROMPT\n%s\nEND SYSTEM PROMPT\n%s"
                    % (system_prompt, prompt)
                )
            data = {
                "prompt": full_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.9,
                "stream": True,
            }
            if not _is_openai_compatible(self.config) or seed_val:
                try:
                    data["seed"] = int(seed_val) if seed_val else 10
                except (TypeError, ValueError):
                    data["seed"] = 10

        if model:
            data["model"] = model

        json_data = json.dumps(data).encode("utf-8")
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
            
        debug_log("Request data: %s" % json.dumps(data, indent=2), context="API")
        return "POST", path, json_data, self._headers()

    def extract_content_from_response(self, chunk, api_type="completions"):
        """Extract text content and optional thinking from API response chunk."""
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

        if api_type == "chat":
            content = (delta.get("content") or "") if delta else ""
        else:
            content = (choice.get("text") or "") if choice else ""

        thinking = _extract_thinking_from_delta(chunk)

        return content, finish_reason, thinking, delta

    def make_chat_request(self, messages, max_tokens=512, tools=None, stream=False):
        """Build a chat completions request from a full messages array."""
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = 512

        endpoint = self._endpoint()
        api_path = self._api_path()
        url = endpoint + api_path + "/chat/completions"
        model_name = self.config.get("model", "")
        temperature = self.config.get("temperature", 0.5)

        data = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stream": stream,
        }
        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            data["parallel_tool_calls"] = False

        json_data = json.dumps(data).encode("utf-8")
        init_logging(self.ctx)
        debug_log(
            "=== Chat Request (tools=%s, stream=%s) ===" % (bool(tools), stream),
            context="API",
        )
        debug_log("URL: %s" % url, context="API")
        debug_log("Messages: %s" % json.dumps(messages, indent=2), context="API")
        
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
            
        return "POST", path, json_data, self._headers()
            
    def make_image_request(self, prompt, model=None, width=1024, height=1024, source_image=None, image_url=None):
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
        if image_url:
            data["image_url"] = image_url
        elif source_image:
            data["image_url"] = "data:image/png;base64," + source_image

        json_data = json.dumps(data).encode("utf-8")
        init_logging(self.ctx)
        debug_log("=== Image Request ===", context="API")
        debug_log("URL: %s" % url, context="API")
        debug_log("Data: %s" % json.dumps(data, indent=2), context="API")
        
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
            
        return "POST", path, json_data, self._headers()

    def stream_completion(
        self,
        prompt,
        system_prompt,
        max_tokens,
        api_type,
        append_callback,
        append_thinking_callback=None,
        stop_checker=None,
        status_callback=None,
    ):
        """Stream a completion/chat response via callbacks."""
        method, path, body, headers = self.make_api_request(
            prompt, system_prompt, max_tokens, api_type=api_type
        )
        self.stream_request(
            method, path, body, headers,
            api_type,
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
        api_type,
        on_content,
        on_thinking=None,
        on_delta=None,
        stop_checker=None,
        _retry=True,
    ):
        """Common low-level streaming engine."""
        init_logging(self.ctx)
        debug_log("=== Starting streaming loop (persistent) ===", context="API")
        debug_log("Request Path: %s" % path, context="API")

        last_finish_reason = None
        conn = self._get_connection()
        
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            
            if response.status != 200:
                err_body = response.read().decode("utf-8", errors="replace")
                debug_log("API Error %d: %s" % (response.status, err_body), context="API")
                # Close on error to be safe
                self._close_connection()
                raise Exception(_format_http_error_response(response.status, response.reason, err_body))

            try:
                # Use a flag to stop logical processing but keep reading to exhaust the stream
                content_finished = False
                # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                last_contents = collections.deque(maxlen=REPEATED_STREAMING_CHUNK_LIMIT)
                for line in response:
                    line_str = line.strip()
                    if not line_str:
                        continue

                    # SSE comments (like : OPENROUTER PROCESSING) or heartbeats
                    if line_str.startswith(b":"):
                        continue

                    if not line_str.startswith(b"data:"):
                        continue
                    
                    # Payload is everything after "data:" (with or without space)
                    idx = line_str.find(b":") + 1
                    payload = line_str[idx:].decode("utf-8").strip()
                    
                    if payload == "[DONE]":
                        debug_log("streaming_loop: [DONE] received", context="API")
                        content_finished = True
                        continue
                    
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        debug_log("streaming_loop: JSON decode error in payload: %s" % payload, context="API")
                        continue
                    if chunk is None:
                        continue

                    # Log all chunks for debugging, even after content_finished
                    # (this might contain 'usage' data)
                    if "usage" in chunk:
                        debug_log("streaming_loop: received usage: %s" % chunk["usage"], context="API")

                    if content_finished:
                        continue

                    if stop_checker and stop_checker():
                        debug_log("streaming_loop: Stop requested.", context="API")
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
                        self.extract_content_from_response(chunk, api_type)
                    )

                    # LiteLLM: streaming_handler.py ~L736 "finish_reason: error, no content string given"
                    if finish_reason == "error":
                        raise Exception("Stream ended with finish_reason=error")

                    if thinking and on_thinking:
                        on_thinking(thinking)
                    if content and on_content:
                        on_content(content)
                        # LiteLLM: streaming_handler.py ~L198 safety_checker(), issue #5158
                        last_contents.append(content)
                        if (len(last_contents) == REPEATED_STREAMING_CHUNK_LIMIT
                                and len(content) > 2
                                and all(c == last_contents[0] for c in last_contents)):
                            raise Exception(
                                "The model is repeating the same chunk (infinite loop). "
                                "Try again or use a different model."
                            )
                    if delta and on_delta:
                        _normalize_delta(delta)
                        on_delta(delta)

                    if finish_reason:
                        debug_log("streaming_loop: logical finish_reason=%s" % finish_reason, context="API")
                        last_finish_reason = finish_reason
            finally:
                # Ensure the entire response body is read so the connection is reusable.
                try:
                    remaining = response.read()
                    if remaining:
                        debug_log("Consumed extra %d bytes after loop" % len(remaining), context="API")
                except:
                    pass
                # Honor Connection: close so we don't try to reuse when the server closed.
                conn_hdr = (response.getheader("Connection") or "").strip().lower()
                if conn_hdr == "close":
                    self._close_connection()

        except (http.client.HTTPException, socket.error, OSError) as e:
            debug_log("Connection error, closing: %s" % e, context="API")
            self._close_connection()
            err_msg = format_error_message(e)
            if _retry:
                debug_log("Retrying streaming request once on fresh connection", context="API")
                return self._run_streaming_loop(
                    method, path, body, headers, api_type,
                    on_content=on_content,
                    on_thinking=on_thinking,
                    on_delta=on_delta,
                    stop_checker=stop_checker,
                    _retry=False,
                )
            debug_log("Connection retry failed: %s" % err_msg, context="API")
            raise Exception(err_msg)
        except Exception as e:
            self._close_connection() # Reset on any other error too
            err_msg = format_error_message(e)
            debug_log("ERROR in _run_streaming_loop: %s -> %s" % (e, err_msg), context="API")
            raise Exception(err_msg)

        return last_finish_reason

    def stream_request(
        self,
        method,
        path,
        body,
        headers,
        api_type,
        append_callback,
        append_thinking_callback=None,
        stop_checker=None,
    ):
        """Stream a completion/chat response and append chunks via callbacks."""
        self._run_streaming_loop(
            method,
            path,
            body,
            headers,
            api_type,
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
            "chat",
            append_callback,
            append_thinking_callback,
            stop_checker=stop_checker,
        )

    def request_with_tools(self, messages, max_tokens=512, tools=None, body_override=None):
        """Non-streaming chat request. Returns parsed response dict. body_override: optional str/bytes to use as request body (e.g. for modalities)."""
        method, path, body, headers = self.make_chat_request(
            messages, max_tokens, tools=tools, stream=False
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
                    debug_log("API Error %d: %s" % (response.status, err_body), context="API")
                    self._close_connection()
                    raise Exception(_format_http_error_response(response.status, response.reason, err_body))
                result = json.loads(response.read().decode("utf-8"))
                break
            except (http.client.HTTPException, socket.error, OSError) as e:
                debug_log("Connection error, closing: %s" % e, context="API")
                self._close_connection()
                if attempt == 0:
                    debug_log("Retrying request_with_tools once on fresh connection", context="API")
                    continue
                debug_log("Connection retry failed: %s" % format_error_message(e), context="API")
                raise Exception(format_error_message(e))
            except Exception as e:
                err_msg = format_error_message(e)
                debug_log("request_with_tools ERROR: %s -> %s" % (e, err_msg), context="API")
                raise Exception(err_msg)

        debug_log("=== Tool response: %s" % json.dumps(result, indent=2), context="API")

        choice = result.get("choices", [{}])[0] if result.get("choices") else {}
        message = choice.get("message") or result.get("message") or {}
        # LiteLLM: same Mistral/Azure compat as _normalize_delta (streaming_handler.py ~L820, ~L847, ~L853)
        _normalize_delta(message)
        finish_reason = choice.get("finish_reason") or result.get("done_reason")

        raw_content = message.get("content")
        content = _normalize_message_content(raw_content)
        images = message.get("images") or []

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": message.get("tool_calls"),
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
        debug_log("stream_request_with_tools: building request (%d messages)..." % len(messages), context="API")
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
                "chat",
                on_content=append_callback,
                on_thinking=append_thinking_callback,
                on_delta=lambda d: accumulate_delta(message_snapshot, d),
                stop_checker=stop_checker,
            )
        except Exception as e:
            err_msg = format_error_message(e)
            debug_log("stream_request_with_tools ERROR: %s -> %s" % (e, err_msg), context="API")
            raise Exception(err_msg)

        # LiteLLM: streaming_handler.py ~L970 finish_reason_handler() "## if tool use"
        if last_finish_reason == "stop" and message_snapshot.get("tool_calls"):
            last_finish_reason = "tool_calls"

        raw_content = message_snapshot.get("content")
        content = _normalize_message_content(raw_content)
        tool_calls = message_snapshot.get("tool_calls")

        return {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": last_finish_reason,
            "usage": message_snapshot.get("usage", {}),
        }

    def chat_completion_sync(self, messages, max_tokens=512):
        """
        Synchronous chat completion (no streaming, no tools).
        Returns the assistant message content string.
        """
        result = self.request_with_tools(
            messages, max_tokens=max_tokens, tools=None
        )
        return result.get("content") or ""
