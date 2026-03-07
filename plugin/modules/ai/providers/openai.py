"""OpenAI-compatible LLM provider.

Wraps the LlmClient HTTP engine to implement the LlmProvider ABC.
Works with OpenAI, OpenRouter, LM Studio, Together AI, Mistral, etc.
"""

import collections
import json
import logging
import http.client
import socket
import ssl
import urllib.parse
import urllib.request
import base64
import re
import tempfile

from plugin.modules.ai.provider_base import LlmProvider, ImageProvider

log = logging.getLogger("localwriter.ai_openai")

REPEATED_CHUNK_LIMIT = 20


def _unverified_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _normalize_delta(delta):
    """Normalize delta quirks (Mistral role=None, Azure arguments=None)."""
    if not isinstance(delta, dict):
        return
    if "role" in delta and delta["role"] is None:
        delta["role"] = "assistant"
    for tc in delta.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        if tc.get("type") is None:
            tc["type"] = "function"
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("arguments") is None:
            fn["arguments"] = ""


def _extract_thinking(delta):
    """Extract reasoning/thinking text from a stream delta."""
    for field in ("reasoning_content", "thought", "thinking"):
        val = delta.get(field)
        if isinstance(val, str) and val:
            return val
    details = delta.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("summary") or "")
        if parts:
            return "".join(parts)
    return ""


def _format_http_error(status, reason, body):
    base = "HTTP Error %d: %s" % (status, reason)
    if not body or not body.strip():
        return base
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            return base + ". " + detail
    except (json.JSONDecodeError, TypeError):
        pass
    return base + ". " + body.strip()[:400]


class OpenAICompatProvider(LlmProvider):
    """OpenAI-compatible chat completions provider."""

    name = "ai_openai"

    def __init__(self, config_proxy):
        self._config = config_proxy
        self._conn = None
        self._conn_key = None

    # ── Connectivity check ──────────────────────────────────────────────

    def check(self):
        """TCP probe to the API endpoint (5s timeout)."""
        parsed = urllib.parse.urlparse(self._endpoint())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if (parsed.scheme or "http") == "https" else 80)
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            return (True, "")
        except (socket.timeout, OSError) as e:
            return (False, "Provider unreachable at %s:%d (%s)" % (host, port, e))

    # ── Connection management ──────────────────────────────────────────

    def _endpoint(self):
        return self._config.get("endpoint") or "http://127.0.0.1:5000"

    def _api_path(self):
        return "/v1"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        key = self._config.get("api_key") or ""
        if key:
            h["Authorization"] = "Bearer %s" % key
        return h

    def _timeout(self):
        return self._config.get("request_timeout") or 120

    def _get_conn(self):
        parsed = urllib.parse.urlparse(self._endpoint())
        scheme = (parsed.scheme or "http").lower()
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)

        if self._conn and self._conn_key == key:
            return self._conn

        self.close()
        self._conn_key = key
        timeout = self._timeout()
        if scheme == "https":
            self._conn = http.client.HTTPSConnection(
                host, port, context=_unverified_ssl(), timeout=timeout
            )
        else:
            self._conn = http.client.HTTPConnection(host, port, timeout=timeout)
        return self._conn

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._conn_key = None

    # ── Request building ───────────────────────────────────────────────

    def _build_body(self, messages, tools=None, stream=False, **kwargs):
        model = self._config.get("model") or ""
        max_tok = kwargs.get("max_tokens") or self._config.get("max_tokens") or 4096

        body = {
            "messages": messages,
            "max_tokens": int(max_tok),
            "stream": stream,
        }
        
        # Only include temperature if explicitly provided (e.g. from get_api_config)
        # or passed via kwargs. If -1 was used, it's omitted from config and thus here.
        temp = kwargs.get("temperature")
        if temp is None:
            temp = self._config.get("temperature")
            
        if temp is not None:
            body["temperature"] = float(temp)
        if model:
            body["model"] = model
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def _request_path(self):
        return self._api_path() + "/chat/completions"

    # ── LlmProvider interface ──────────────────────────────────────────

    def stream(self, messages, tools=None, **kwargs):
        """Yield streaming chunks as dicts.

        Each yielded chunk has at least:
        - content: str (text delta)
        - thinking: str (reasoning delta)
        - delta: dict (raw delta for tool-call accumulation)
        - finish_reason: str | None
        """
        body = self._build_body(messages, tools=tools, stream=True, **kwargs)
        data = json.dumps(body).encode("utf-8")
        path = self._request_path()
        headers = self._headers()

        endpoint = self._endpoint()
        log.info("stream: %s%s model=%s tools=%d msgs=%d",
                 endpoint, path, body.get("model", "?"),
                 len(body.get("tools") or []), len(messages))

        conn = self._get_conn()
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
        except socket.timeout:
            self.close()
            raise RuntimeError(
                "Request timed out after %ds" % self._timeout())
        except (http.client.HTTPException, socket.error, OSError) as e:
            log.warning("stream: connection error, retrying: %s", e)
            self.close()
            conn = self._get_conn()
            try:
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
            except socket.timeout:
                self.close()
                raise RuntimeError(
                    "Request timed out after %ds" % self._timeout())

        log.info("stream: response status=%d", resp.status)

        if resp.status != 200:
            err = resp.read().decode("utf-8", errors="replace")
            self.close()
            raise RuntimeError(_format_http_error(resp.status, resp.reason, err))

        try:
            last_chunks = collections.deque(maxlen=REPEATED_CHUNK_LIMIT)
            for line in resp:
                line = line.strip()
                if not line or line.startswith(b":") or not line.startswith(b"data:"):
                    continue
                payload = line[line.find(b":") + 1 :].decode("utf-8").strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not chunk:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                _normalize_delta(delta)
                content = delta.get("content") or ""
                thinking = _extract_thinking(chunk)
                finish = choice.get("finish_reason")

                if finish == "error":
                    raise RuntimeError("Stream ended with finish_reason=error")

                # Infinite loop safety
                if content:
                    last_chunks.append(content)
                    if (
                        len(last_chunks) == REPEATED_CHUNK_LIMIT
                        and len(content) > 2
                        and all(c == last_chunks[0] for c in last_chunks)
                    ):
                        raise RuntimeError("Model repeating same chunk (infinite loop)")

                yield {
                    "content": content,
                    "thinking": thinking,
                    "delta": delta,
                    "finish_reason": finish,
                }
        finally:
            try:
                resp.read()
            except Exception:
                pass
            conn_hdr = (resp.getheader("Connection") or "").lower()
            if conn_hdr == "close":
                self.close()

    def complete(self, messages, tools=None, **kwargs):
        """Non-streaming completion. Returns full response dict."""
        body = self._build_body(messages, tools=tools, stream=False, **kwargs)
        data = json.dumps(body).encode("utf-8")
        path = self._request_path()
        headers = self._headers()

        conn = self._get_conn()
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
        except socket.timeout:
            self.close()
            raise RuntimeError(
                "Request timed out after %ds" % self._timeout())
        except (http.client.HTTPException, socket.error, OSError):
            self.close()
            conn = self._get_conn()
            try:
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
            except socket.timeout:
                self.close()
                raise RuntimeError(
                    "Request timed out after %ds" % self._timeout())

        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            self.close()
            raise RuntimeError(_format_http_error(resp.status, resp.reason, raw))

        result = json.loads(raw)
        choices = result.get("choices", [])
        if not choices:
            return {"content": None, "tool_calls": None, "finish_reason": None}

        msg = choices[0].get("message", {})
        return {
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "finish_reason": choices[0].get("finish_reason"),
        }

    def supports_tools(self):
        return True

    def supports_vision(self):
        return False

    def get_status(self):
        model = self._config.get("model") or ""
        return {"ready": True, "message": "Ready", "model": model}


class EndpointImageProvider(ImageProvider):
    """Image generation via the configured OpenAI-compatible endpoint."""

    name = "endpoint"

    def __init__(self, config_proxy):
        self._config = config_proxy

    def generate(self, prompt, width=1024, height=1024, model=None, **kwargs):
        """Generate an image via /v1/images/generations.

        Returns:
            (file_paths: list[str], error: str | None)
        """
        # Create a temporary provider instance for HTTP connection
        provider = OpenAICompatProvider(self._config)
        try:
            model_name = model or self._config.get("model") or ""
            body = {
                "prompt": prompt,
                "n": 1,
                "size": "%dx%d" % (width, height),
                "response_format": "url",
            }
            if model_name:
                body["model"] = model_name

            data = json.dumps(body).encode("utf-8")
            path = provider._api_path() + "/images/generations"
            headers = provider._headers()

            conn = provider._get_conn()
            try:
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
            except Exception:
                provider.close()
                conn = provider._get_conn()
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()

            if resp.status != 200:
                err_body = resp.read().decode("utf-8", errors="replace")
                return [], _format_http_error(resp.status, resp.reason,
                                              err_body)

            result = json.loads(resp.read().decode("utf-8"))

            for img in (result.get("data") or []):
                b64 = img.get("b64_json")
                if b64:
                    return self._save_b64(b64), None
                url = img.get("url")
                if url:
                    if "data:image" in url:
                        match = re.search(
                            r'base64,([A-Za-z0-9+/=]+)', url)
                        if match:
                            return self._save_b64(match.group(1)), None
                    return self._save_url(url), None

            return [], "No image data in response"
        except Exception as e:
            log.exception("Image generation failed")
            return [], str(e)
        finally:
            provider.close()

    @staticmethod
    def _save_b64(b64_data):
        with tempfile.NamedTemporaryFile(
                delete=False, suffix=".png") as tmp:
            tmp.write(base64.b64decode(b64_data))
            return [tmp.name]

    @staticmethod
    def _save_url(url):
        from plugin.modules.http.client import sync_request
        with tempfile.NamedTemporaryFile(
                delete=False, suffix=".webp") as tmp:
            tmp.write(sync_request(url, parse_json=False))
            return [tmp.name]
