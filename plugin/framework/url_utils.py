# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
URL parsing utilities for WriterAgent.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

LIBREPY_DISPATCH_PROTOCOL = "org.extension.librepy:"


def matches_librepy_dispatch_url(url: Any) -> bool:
    """Return True when *url* is a LibrePy menu/protocol dispatch URL."""
    proto = str(getattr(url, "Protocol", None) or "")
    if proto.startswith("org.extension.librepy"):
        return True
    complete = str(getattr(url, "Complete", None) or "")
    return complete.startswith(LIBREPY_DISPATCH_PROTOCOL)


def dispatch_command_from_url(url: Any, *, protocol_prefix: str = LIBREPY_DISPATCH_PROTOCOL) -> str:
    """Extract handler command from a LibreOffice dispatch URL.

    LO usually sets ``Path`` (e.g. ``main.settings``). Some dispatch paths only
    populate ``Complete`` (``org.extension.librepy:main.settings``); derive Path
    from that so menu handlers still run.
    """
    path = str(getattr(url, "Path", None) or "").strip().lstrip("/")
    if path:
        return path
    complete = str(getattr(url, "Complete", None) or "").strip()
    if complete.startswith(protocol_prefix):
        return complete[len(protocol_prefix) :].lstrip("/")
    if ":" in complete:
        return complete.split(":", 1)[1].lstrip("/")
    return complete


def _is_zai_host(url):
    """True when URL targets Z.ai (general or coding-plan API)."""
    url_lower = (url or "").lower()
    return "api.z.ai" in url_lower or "z.ai" in url_lower


def _zai_url_path(url):
    """Normalized path without trailing slash (empty string when bare host)."""
    return (urllib.parse.urlparse(url or "").path or "").rstrip("/")


def get_api_version_suffix(url, is_openwebui=False):
    """Return the API version suffix (e.g. '/v1', '/v4', '/api/paas/v4') for a given endpoint URL."""
    if is_openwebui:
        return "/api"
    # Z.ai: bare host uses general OpenAI base (/api/paas/v4); deeper paths append /v4 only.
    if _is_zai_host(url):
        if _zai_url_path(url) in ("", "/"):
            return "/api/paas/v4"
        return "/v4"
    return "/v1"


def normalize_endpoint_url(url, is_openwebui=False):
    """Clean up endpoint URL: strip whitespace, trailing slashes, and domain-specific version suffixes."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    # Remove trailing /
    while url.endswith("/"):
        url = url[:-1]

    # Remove the version suffix we expect to add back (e.g. /v1, /v4, /api/paas/v4, /api)
    suffix = get_api_version_suffix(url, is_openwebui=is_openwebui)
    if url.lower().endswith(suffix):
        url = url[:-len(suffix)]
    elif _is_zai_host(url) and url.lower().endswith("/v4"):
        # Legacy preset stored https://api.z.ai/v4 before general base was /api/paas/v4.
        url = url[:-3]
    elif url.lower().endswith("/v1"):
        # Always strip /v1 as a fallback for custom endpoints
        url = url[:-3]

    return url

def get_url_hostname(url):
    """Return hostname from URL safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.hostname or ""
    except ValueError:
        return ""

def get_url_domain(url):
    """Return 'example.com' from 'https://api.example.com/v1'."""
    host = get_url_hostname(url)
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host

def get_url_path(url):
    """Return path from URL safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.path or ""
    except ValueError:
        return ""

def get_url_query_dict(url):
    """Return query parameters as dict (values are lists)."""
    if not url:
        return {}
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.parse_qs(parsed.query)
    except ValueError:
        return {}

def get_url_path_and_query(url):
    """Return path + query string from URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            return f"{path}?{parsed.query}"
        return path
    except ValueError:
        return "/"

def is_pdf_url(url):
    """Check for .pdf in the URL path safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.path or "").lower().endswith(".pdf")
    except ValueError:
        return False
