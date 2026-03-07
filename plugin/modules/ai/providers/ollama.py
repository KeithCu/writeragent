"""Ollama LLM provider.

Extends the OpenAI-compatible provider with Ollama-specific defaults.
Ollama exposes an OpenAI-compatible API at /v1/chat/completions since v0.1.14.
"""

import http.client
import json
import logging
import socket
import threading
import urllib.parse

from plugin.modules.ai.providers.openai import OpenAICompatProvider

log = logging.getLogger("writeragent.ai_ollama")


class OllamaProvider(OpenAICompatProvider):
    """Ollama-specific provider.

    Inherits everything from OpenAICompatProvider — Ollama's OpenAI
    compatibility layer uses the same /v1/chat/completions endpoint.
    """

    name = "ai_ollama"

    def __init__(self, config_proxy):
        super().__init__(config_proxy)
        self._status_lock = threading.Lock()
        self._status = "unknown"  # unknown | loading | ready | error
        self._status_message = ""

    def _endpoint(self):
        return self._config.get("endpoint") or "http://localhost:11434"

    def _timeout(self):
        # Ollama may need longer for initial model loading
        return self._config.get("request_timeout") or 300

    def supports_tools(self):
        # Most Ollama models support tool calling
        return True

    def supports_vision(self):
        # Some Ollama models support vision (llava, etc.)
        return False

    # ── Connectivity check ──────────────────────────────────────────────

    def check(self):
        """Check Ollama is reachable and the configured model exists."""
        parsed = urllib.parse.urlparse(self._endpoint())
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434

        # 1. TCP probe
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
        except (socket.timeout, OSError) as e:
            return (False, "Ollama unreachable at %s:%d (%s)" % (host, port, e))

        # 2. Verify model exists
        model = self._config.get("model") or ""
        if not model:
            return (True, "")
        try:
            status, raw = self._ollama_request(
                "POST", "/api/show", body={"name": model}, timeout=5)
            if status != 200:
                return (False, "Model '%s' not found in Ollama" % model)
        except Exception as e:
            return (False, "Ollama model check failed: %s" % e)

        return (True, "")

    # ── Warmup / status ───────────────────────────────────────────────

    def _ollama_request(self, method, path, body=None, timeout=30):
        """Low-level HTTP request to the Ollama native API."""
        parsed = urllib.parse.urlparse(self._endpoint())
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434
        scheme = (parsed.scheme or "http").lower()

        if scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                host, port, context=ctx, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

        headers = {"Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body else None
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
        finally:
            conn.close()

    def warmup(self):
        """Pre-load the model into Ollama's memory.

        Calls POST /api/generate with an empty prompt and keep_alive to
        trigger model loading without generating tokens.
        """
        model = self._config.get("model") or ""
        if not model:
            return

        with self._status_lock:
            self._status = "loading"
            self._status_message = "Loading model %s..." % model

        log.info("warmup: loading model %s", model)
        try:
            status, raw = self._ollama_request(
                "POST", "/api/generate",
                body={"model": model, "keep_alive": "5m"},
                timeout=300,
            )
            if status == 200:
                with self._status_lock:
                    self._status = "ready"
                    self._status_message = "Ready"
                log.info("warmup: model %s loaded", model)
            else:
                msg = "Warmup failed (HTTP %d)" % status
                try:
                    data = json.loads(raw)
                    msg = data.get("error", msg)
                except Exception:
                    pass
                with self._status_lock:
                    self._status = "error"
                    self._status_message = msg
                log.warning("warmup: %s", msg)
        except socket.timeout:
            with self._status_lock:
                self._status = "error"
                self._status_message = "Warmup timed out"
            log.warning("warmup: timed out for model %s", model)
        except Exception as e:
            with self._status_lock:
                self._status = "error"
                self._status_message = str(e)
            log.warning("warmup: error for model %s: %s", model, e)

    def is_ready(self):
        """Check if the model is loaded in Ollama's memory."""
        with self._status_lock:
            if self._status == "ready":
                return True
            if self._status == "loading":
                return False

        # For unknown/error status, probe Ollama
        model = self._config.get("model") or ""
        if not model:
            return True

        try:
            status, raw = self._ollama_request("GET", "/api/ps", timeout=5)
            if status == 200:
                data = json.loads(raw)
                models = data.get("models") or []
                for m in models:
                    if m.get("name", "").startswith(model) or \
                       m.get("model", "").startswith(model):
                        with self._status_lock:
                            self._status = "ready"
                            self._status_message = "Ready"
                        return True
        except Exception:
            pass
        return False

    def get_status(self):
        model = self._config.get("model") or ""
        with self._status_lock:
            return {
                "ready": self._status == "ready",
                "message": self._status_message or self._status,
                "model": model,
            }
