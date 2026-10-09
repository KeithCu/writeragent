# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Centralized provider and endpoint detection heuristics.

This module is the single source of truth (as of the 2026 framework janitor
effort) for answering questions like:

- "Which provider does this endpoint URL belong to?"
- "Is this a local / LAN host where self-signed certs are common?"
- "Should we treat this as OpenRouter even if the hostname is a custom proxy?"

### Why this module exists (the problem it solves)

Before consolidation the same (or very similar) string matching logic was
scattered across at least these locations:

- plugin/framework/client/model_fetcher.py          (imports get_provider_from_endpoint)
- plugin/framework/client/auth.py                    (_resolve_provider_id)
- plugin/framework/client/llm_client.py              (is_openrouter_endpoint)
- plugin/framework/client/requests.py / request_controls.py (is_local_host)
- plugin/framework/client/errors.py (historical)     (connection refused / DNS strings in the mapper)
- plugin/framework/config.py                         (is_openrouter + openwebui string checks)
- plugin/framework/url_utils.py                      (z.ai / openwebui special cases)

This made it easy for the list of known providers (Ollama, LM Studio, OpenRouter,
Groq, Cerebras, Z.ai, etc.) to get out of sync, and for local-host heuristics to
drift between the SSL fallback path and the friendly error messages.

### Design principles for this module (first pass)

- Pure functions only — no I/O, no config side effects.
- Detection in this module is the host matcher. auth.PROVIDERS is authentication
  behavior (header style, model id style), not a second host table.
- First-pass scope: the highest-ROI, lowest-risk functions only
  (get_provider_from_endpoint, is_local_host, is_openrouter_endpoint).
- Audio capability heuristics, full preset lists, and shim selection stay in
  their original homes for now (they can be pulled in later passes).
- All existing callers continue to get identical results.

New providers or detection rules should be added in one place and will
automatically benefit error messages, auth, model fetching, local SSL handling,
and logging.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Optional

from plugin.framework.errors import ConfigError
from plugin.framework.url_utils import get_url_hostname, normalize_endpoint_url


def get_provider_from_endpoint(endpoint: str) -> Optional[str]:
    """Return a canonical provider key for DEFAULT_MODELS / auth based on the endpoint URL.

    This is the single implementation after the 2026 provider heuristic consolidation.
    It used to live (in slightly different forms) in model_fetcher.py and was
    duplicated in spirit inside auth.py's _resolve_provider_id.

    Returns None for completely unknown endpoints (they become "custom").
    """
    if not endpoint:
        return None

    url = normalize_endpoint_url(endpoint).lower()
    host = get_url_hostname(url).lower()
    # ``ParseResult.port`` raises ValueError for ``:1a34`` and for ports
    # outside 0–65535; urllib checks the port only when ``.port`` is read.
    # ``urlparse`` itself also raises ValueError for an unmatched bracket
    # (``http://[::1``, ``http://[]/v1``) before ``.port``. Settings and
    # catalog code call this on the configured endpoint, so a bad port or
    # bracket URL is ConfigError, the same contract as other invalid settings.
    try:
        port = urllib.parse.urlparse(url).port
    except ValueError as exc:
        raise ConfigError("Invalid URL port", code="CONFIG_INVALID_URL", details={"endpoint": url.split("?", 1)[0]}) from exc

    def _host_is(*names: str) -> bool:
        # Hostname equality, not a substring of the whole URL. "ollama" inside
        # notollama.example and "z.ai" inside z.ai.example used to match.
        return any(host == name or host.endswith("." + name) for name in names)

    # Order matters for some overlaps (e.g. openrouter before generic openai-compatible)
    if _host_is("openrouter.ai"):
        return "openrouter"
    if _host_is("together.ai", "together.xyz"):
        return "together"
    # LAN addresses (``::1``, RFC1918) on the Ollama port are the app.
    # A public host on 11434 stays custom. Hostname equality, not a substring.
    if _host_is("ollama") or (is_local_host(host) and port == 11434):
        return "ollama"
    if _host_is("api.mistral.ai"):
        return "mistral"
    if _host_is("api.openai.com"):
        return "openai"
    if _host_is("api.deepseek.com"):
        return "deepseek"
    if _host_is("api.groq.com"):
        return "groq"
    if _host_is("api.cerebras.ai"):
        return "cerebras"
    if _host_is("api.perplexity.ai"):
        return "perplexity"
    if _host_is("api.x.ai"):
        return "xai"
    if _host_is("api.anthropic.com"):
        return "anthropic"
    if _host_is("generativelanguage.googleapis.com"):
        return "google"
    if is_local_host(host) and port == 1234:
        return "lmstudio"
    if _host_is("z.ai"):
        return "zai"
    if _host_is("integrate.api.nvidia.com", "api.nvidia.com"):
        return "nvidia"

    return None


def is_local_host(host: str) -> bool:
    """Heuristic: is this host a localhost / LAN address where self-signed TLS is common?

    Consolidated here in the 2026 janitor effort. Previously lived as the private
    private helpers in ssl_helpers.py and had near-duplicate string checks in the
    (now centralized) error message mapper in errors.py.

    Used by:
    - Local HTTPS certificate fallback logic (llm_client + requests)
    - Friendly error messages for "Connection refused / local AI server"
    - Any future "treat this as dev / insecure by default" decisions
    """
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
        return False


def is_openrouter_endpoint(endpoint: str, explicit_is_openrouter: bool | None = False) -> bool:
    """Should we treat this endpoint as OpenRouter (for extra headers, model ids, etc.)?

    Handles both the explicit config flag (users with a custom proxy that speaks
    the OpenRouter API) and the normal hostname heuristic.

    Consolidated during the provider detection cleanup so the string check no
    longer lives in config.py, llm_client.py, and the old get_provider_from_endpoint.
    """
    if explicit_is_openrouter:
        return True
    if not endpoint:
        return False
    # Hostname equality, same rule as get_provider_from_endpoint. A path or
    # query that merely contains "openrouter.ai" is not this provider.
    # Custom proxies set the explicit flag.
    return get_provider_from_endpoint(endpoint) == "openrouter"


def is_openwebui_endpoint(endpoint: str, explicit_is_openwebui: bool | None = False) -> bool:
    """True for an Open WebUI host, or when the config flag is set.

    Hostname only. A path or query that merely contains "openwebui" is not
    this product. Custom proxies set the explicit flag.
    """
    if explicit_is_openwebui:
        return True
    if not endpoint:
        return False
    host = get_url_hostname(normalize_endpoint_url(endpoint)).lower()
    # A label, not a substring. ``notopenwebui.example`` is not this product.
    # ``chat.openwebui.example`` still is.
    labels = [part for part in host.split(".") if part]
    return "openwebui" in labels or "open-webui" in labels
