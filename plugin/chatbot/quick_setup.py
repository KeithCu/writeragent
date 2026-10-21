"""Background inference server discovery and connection verification engine."""

from __future__ import annotations

import json
import logging
import socket
import urllib.request
import urllib.error
from typing import Any

from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT
from plugin.framework.url_utils import get_api_version_suffix, normalize_endpoint_url


log = logging.getLogger(__name__)

# Catalog of local server definitions
LOCAL_SERVER_PROBES: list[dict[str, Any]] = [
    {"name": "Ollama", "port": 11434, "path": "/api/tags", "kind": "ollama", "url": "http://localhost:11434"},
    {"name": "LM Studio", "port": 1234, "path": "/v1/models", "kind": "openai", "url": "http://localhost:1234"},
    {"name": "llama.cpp (llama-server)", "port": 8080, "path": "/v1/models", "kind": "openai", "url": "http://localhost:8080"},
    {"name": "vLLM", "port": 8000, "path": "/v1/models", "kind": "openai", "url": "http://localhost:8000"},
    {"name": "LiteLLM Proxy", "port": 4000, "path": "/v1/models", "kind": "openai", "url": "http://localhost:4000"},
    {"name": "KoboldCPP", "port": 5001, "path": "/v1/models", "kind": "openai", "url": "http://localhost:5001"},
    {"name": "Jan.ai", "port": 1337, "path": "/v1/models", "kind": "openai", "url": "http://localhost:1337"},
    {"name": "Backyard AI", "port": 13370, "path": "/v1/models", "kind": "openai", "url": "http://localhost:13370"},
    {"name": "Msty", "port": 10240, "path": "/v1/models", "kind": "openai", "url": "http://localhost:10240"},
    {"name": "Open WebUI", "port": 3000, "path": "/api/models", "kind": "openai", "url": "http://localhost:3000"},
    {"name": "AnythingLLM", "port": 3001, "path": "/v1/models", "kind": "openai", "url": "http://localhost:3001"},
    {"name": "TabbyAPI", "port": 5000, "path": "/v1/models", "kind": "openai", "url": "http://localhost:5000"},
    {"name": "SGLang", "port": 30000, "path": "/v1/models", "kind": "openai", "url": "http://localhost:30000"},
    {"name": "Xinference", "port": 9997, "path": "/v1/models", "kind": "openai", "url": "http://localhost:9997"},
    {"name": "exo Cluster", "port": 52415, "path": "/v1/models", "kind": "openai", "url": "http://localhost:52415"},
    {"name": "MistralRS", "port": 8080, "path": "/v1/models", "kind": "openai", "url": "http://localhost:8080"},
    {"name": "Aphrodite", "port": 2242, "path": "/v1/models", "kind": "openai", "url": "http://localhost:2242"},
    {"name": "Llamafile", "port": 8080, "path": "/v1/models", "kind": "openai", "url": "http://localhost:8080"},
    {"name": "TGI", "port": 8080, "path": "/info", "kind": "openai", "url": "http://localhost:8080"},
]


def _check_port_open(host: str, port: int, timeout_sec: float = 0.15) -> bool:
    """Non-blocking TCP socket check."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_sec)
    try:
        sock.connect((host, port))
        sock.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def probe_local_servers() -> list[dict[str, Any]]:
    """Scan candidate ports and query models from active servers synchronously."""
    detected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for probe in LOCAL_SERVER_PROBES:
        port = probe["port"]
        url = probe["url"]
        if url in seen_urls:
            continue

        if not _check_port_open("127.0.0.1", port):
            continue

        endpoint_url = f"http://127.0.0.1:{port}{probe['path']}"
        try:
            req = urllib.request.Request(endpoint_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=0.6) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models: list[str] = []
                    if probe["kind"] == "ollama":
                        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                    elif isinstance(data, dict) and "data" in data:
                        models = [m.get("id", "") for m in data["data"] if isinstance(m, dict) and m.get("id")]
                    elif isinstance(data, list):
                        models = [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]

                    detected.append({
                        "id": probe["name"].lower().replace(" ", "_"),
                        "name": probe["name"],
                        "display_name": f"(Detected) Local ({probe['name']})",
                        "url": url,
                        "models": models,
                        "signup_url": "",
                        "is_local": True,
                    })
                    seen_urls.add(url)
        except Exception as e:
            log.debug("Probe failed for %s on port %d: %s", probe["name"], port, e)
            # Port was open even if HTTP probe failed; still offer as detected
            detected.append({
                "id": probe["name"].lower().replace(" ", "_"),
                "name": probe["name"],
                "display_name": f"(Detected) Local ({probe['name']})",
                "url": url,
                "models": [],
                "signup_url": "",
                "is_local": True,
            })
            seen_urls.add(url)

    return detected


def check_endpoint_connection(endpoint: str, api_key: str = "") -> tuple[bool, str, list[str]]:
    """Test connection to endpoint and return (success, message, models_list)."""
    import time
    if not endpoint:
        return False, "Endpoint URL cannot be empty.", []

    endpoint_clean = endpoint.rstrip("/")
    # Build models probe URL
    if ":11434" in endpoint_clean:
        models_url = f"{endpoint_clean}/api/tags"
    else:
        norm_endpoint = normalize_endpoint_url(endpoint_clean)
        suffix = get_api_version_suffix(norm_endpoint)
        models_url = f"{norm_endpoint}{suffix}/models"

    headers = {"User-Agent": USER_AGENT}
    if "openrouter.ai" in endpoint_clean:
        headers["HTTP-Referer"] = APP_REFERER
        headers["X-Title"] = APP_TITLE

    if api_key:
        if "anthropic.com" in endpoint_clean:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    start_time = time.time()
    try:
        req = urllib.request.Request(models_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            elapsed_ms = int((time.time() - start_time) * 1000)
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models: list[str] = []
                if ":11434" in endpoint_clean and "models" in data:
                    models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                elif isinstance(data, dict) and "data" in data:
                    models = [m.get("id", "") for m in data["data"] if isinstance(m, dict) and m.get("id")]
                elif isinstance(data, list):
                    models = [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]

                msg = f"✓ Connected successfully! ({elapsed_ms}ms, {len(models)} models available)"
                return True, msg, models
            return False, f"Server returned HTTP {resp.status}.", []
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Authentication failed (401 Unauthorized). Please check your API key.", []
        if e.code == 404:
            return False, "Models endpoint not found (404). Check the endpoint URL.", []
        return False, f"HTTP Error {e.code}: {e.reason}", []
    except urllib.error.URLError as e:
        return False, f"Connection failed: {e.reason}", []
    except Exception as e:
        return False, f"Error: {e}", []
