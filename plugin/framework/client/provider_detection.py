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

- plugin/framework/client/model_fetcher.py          (get_provider_from_endpoint)
- plugin/framework/client/auth.py                    (_resolve_provider_id + host_matches)
- plugin/framework/client/ssl_helpers.py             (_is_local_host - private)
- plugin/framework/client/llm_client.py              (various _is_local_host + provider == "ollama" checks)
- plugin/framework/client/requests.py                (indirect via ssl_helpers)
- plugin/framework/client/errors.py (historical)     (connection refused / DNS strings in the mapper)
- plugin/framework/config.py                         (is_openrouter + openwebui string checks)
- plugin/framework/url_utils.py                      (z.ai / openwebui special cases)

This made it easy for the list of known providers (Ollama, LM Studio, OpenRouter,
Groq, Cerebras, Z.ai, etc.) to get out of sync, and for local-host heuristics to
drift between the SSL fallback path and the friendly error messages.

### Design principles for this module (first pass)

- Pure functions only — no I/O, no config side effects.
- Keep the nice data-driven table in auth.py (PROVIDERS + host_matches) as the
  authority for *authentication behavior*. Detection feeds it, it does not replace it.
- First-pass scope: the highest-ROI, lowest-risk functions only
  (get_provider_from_endpoint, is_local_host, is_openrouter_endpoint).
- Audio capability heuristics, full preset lists, and shim selection stay in
  their original homes for now (they can be pulled in later passes).
- All existing callers continue to get identical results.

New providers or detection rules should be added in one place and will
automatically benefit error messages, auth, model fetching, local SSL handling,
and logging.
"""

import ipaddress
from typing import Optional

from plugin.framework.url_utils import normalize_endpoint_url


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

    # Order matters for some overlaps (e.g. openrouter before generic openai-compatible)
    if "openrouter.ai" in url:
        return "openrouter"
    if "together.xyz" in url:
        return "together"
    if "localhost:11434" in url or "ollama" in url:
        return "ollama"
    if "api.mistral.ai" in url:
        return "mistral"
    if "api.openai.com" in url:
        return "openai"
    if "api.deepseek.com" in url:
        return "deepseek"
    if "api.groq.com" in url:
        return "groq"
    if "api.cerebras.ai" in url:
        return "cerebras"
    if "api.perplexity.ai" in url:
        return "perplexity"
    if "api.x.ai" in url:
        return "xai"
    if "api.anthropic.com" in url:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url:
        return "google"
    if "localhost:1234" in url:
        return "lmstudio"
    if "api.z.ai" in url or "z.ai" in url:
        return "zai"

    return None


def is_local_host(host: str) -> bool:
    """Heuristic: is this host a localhost / LAN address where self-signed TLS is common?

    Consolidated here in the 2026 janitor effort. Previously lived as the private
    _is_local_host in ssl_helpers.py and had near-duplicate string checks in the
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
        pass
    # Single-label hostnames are usually local network names (e.g. "ollama-box").
    return "." not in host


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
    url = normalize_endpoint_url(endpoint).lower()
    return "openrouter.ai" in url
