# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Provider and endpoint detection.

Pure functions, no I/O. ``get_provider_from_endpoint`` maps a URL host to a
canonical provider key, or None when the host is unknown. ``is_local_host``
is the localhost / LAN check. ``is_openrouter_endpoint`` and
``is_openwebui_endpoint`` combine an explicit config flag with that host check.

Host matching lives here. ``auth.PROVIDERS`` is authentication behavior, not
a second host table. Add a provider host in ``get_provider_from_endpoint``.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Optional

from plugin.framework.errors import ConfigError
from plugin.framework.url_utils import get_url_hostname, host_matches, normalize_endpoint_url


# First match wins. Keys are disjoint under host_matches (exact or subdomain).
# The "ollama" hostname is here; port 11434 / 1234 are extra rules below it.
_PROVIDER_HOSTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openrouter", ("openrouter.ai",)),
    ("together", ("together.ai", "together.xyz")),
    ("ollama", ("ollama",)),
    ("mistral", ("api.mistral.ai",)),
    ("openai", ("api.openai.com",)),
    ("deepseek", ("api.deepseek.com",)),
    ("groq", ("api.groq.com",)),
    ("cerebras", ("api.cerebras.ai",)),
    ("perplexity", ("api.perplexity.ai",)),
    ("xai", ("api.x.ai",)),
    ("anthropic", ("api.anthropic.com",)),
    ("google", ("generativelanguage.googleapis.com",)),
    ("zai", ("z.ai",)),
    ("nvidia", ("integrate.api.nvidia.com", "api.nvidia.com")),
)


def get_provider_from_endpoint(endpoint: str) -> Optional[str]:
    """Return a canonical provider key for DEFAULT_MODELS / auth based on the endpoint URL.

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

    # Hostname or subdomain, not a substring (notollama.example, z.ai.example).
    for provider, names in _PROVIDER_HOSTS:
        if host_matches(host, *names):
            return provider
    # LAN addresses (``::1``, RFC1918) on the Ollama port are the app.
    # A public host on 11434 stays custom.
    if is_local_host(host) and port == 11434:
        return "ollama"
    if is_local_host(host) and port == 1234:
        return "lmstudio"
    return None


def is_local_host(host: str) -> bool:
    """Heuristic: is this host a localhost / LAN address where self-signed TLS is common?"""
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
