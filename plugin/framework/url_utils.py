# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
URL parsing utilities for WriterAgent.

HTTP / LLM endpoint helpers live below. Filesystem ``file:`` conversion is a
separate section — do not fold it into ``normalize_endpoint_url``.
"""

# crosshair: off
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

from plugin.framework.constants import EXTENSION_ID_LIBREPY
from plugin.framework.deal_shim import DEAL_MAX_URL, ascii_bounded, deal

LIBREPY_DISPATCH_PROTOCOL = EXTENSION_ID_LIBREPY + ":"


def matches_librepy_dispatch_url(url: Any) -> bool:
    """Return True when *url* is a LibrePy menu/protocol dispatch URL."""
    proto = str(getattr(url, "Protocol", None) or "")
    if proto.startswith(EXTENSION_ID_LIBREPY):
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
    host = get_url_hostname(url).lower()
    return host == "z.ai" or host.endswith(".z.ai")


def _is_google_host(url):
    """True when URL targets Google Gemini API (generativelanguage.googleapis.com)."""
    if not isinstance(url, str):
        return False
    url_lower = url.lower()
    return "generativelanguage.googleapis.com" in url_lower


def _zai_url_path(url):
    """Normalized path without trailing slash (empty string when bare host)."""
    if not isinstance(url, str):
        return ""
    return (urllib.parse.urlparse(url).path or "").rstrip("/")


@deal.pre(lambda url, is_openwebui=False: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str) and result.startswith("/"))
def get_api_version_suffix(url, is_openwebui=False):
    """Return the API version suffix (e.g. '/v1', '/v4', '/api/paas/v4') for a given endpoint URL."""
    if is_openwebui:
        return "/api"
    # Z.ai: bare host uses general OpenAI base (/api/paas/v4); deeper paths append /v4 only.
    if _is_zai_host(url):
        if _zai_url_path(url) in ("", "/"):
            return "/api/paas/v4"
        return "/v4"
    # Google Gemini: OpenAI-compatible API base is /v1beta/openai
    if _is_google_host(url):
        return "/v1beta/openai"
    return "/v1"


@deal.pre(lambda url, is_openwebui=False: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda url, is_openwebui=False, result="": bool(isinstance(url, str) and url.strip()) or result == "")
def normalize_endpoint_url(url, is_openwebui=False):
    """Clean up endpoint URL: strip whitespace, trailing slashes, and domain-specific version suffixes."""
    if type(url) is not str or not url.strip():
        return ""
    url = url.strip()
    # Remove trailing /
    while url.endswith("/"):
        url = url[:-1]

    # Open WebUI chat is {base}/api/chat/completions — strip pasted /api/v1, /api, or /v1 in one pass.
    # (Half-stripping /api/v1 → /api then appending /api again yields /api/api/chat/completions.)
    if is_openwebui:
        lower = url.lower()
        if lower.endswith("/api/v1"):
            return url[: -len("/api/v1")]
        if lower.endswith("/api"):
            return url[: -len("/api")]
        if lower.endswith("/v1"):
            return url[:-3]
        return url

    if _is_google_host(url):
        lower = url.lower()
        if lower.endswith("/v1beta/openai"):
            return url[: -len("/v1beta/openai")]
        if lower.endswith("/v1beta"):
            return url[: -len("/v1beta")]
        if lower.endswith("/v1"):
            return url[:-3]
        return url

    # Remove the version suffix we expect to add back (e.g. /v1, /v4, /api/paas/v4)
    suffix = get_api_version_suffix(url, is_openwebui=False)
    if url.lower().endswith(suffix):
        url = url[: -len(suffix)]
    elif _is_zai_host(url) and url.lower().endswith("/v4"):
        # Legacy preset stored https://api.z.ai/v4 before general base was /api/paas/v4.
        url = url[:-3]
    elif url.lower().endswith("/v1"):
        # Always strip /v1 as a fallback for custom endpoints
        url = url[:-3]

    return url

@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str))
def get_url_hostname(url):
    """Return hostname from URL safely."""
    if type(url) is not str:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.hostname or ""
    except (ValueError, TypeError, AttributeError):
        # urlparse rejects non-str (TypeError); keep "safely" for CrossHair/fuzz inputs.
        return ""


@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str))
def get_url_domain(url):
    """Return 'example.com' from 'https://api.example.com/v1'."""
    host = get_url_hostname(url)
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str))
def get_url_path(url):
    """Return path from URL safely."""
    if type(url) is not str:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.path or ""
    except (ValueError, TypeError, AttributeError):
        return ""


@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, dict))
def get_url_query_dict(url):
    """Return query parameters as dict (values are lists)."""
    if type(url) is not str or not url:
        return {}
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.parse_qs(parsed.query)
    except (ValueError, TypeError, AttributeError):
        return {}


@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, str))
def get_url_path_and_query(url):
    """Return path + query string from URL."""
    if type(url) is not str:
        return "/"
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            return f"{path}?{parsed.query}"
        return path
    except (ValueError, TypeError, AttributeError):
        return "/"


@deal.pre(lambda url: url is None or ascii_bounded(url, DEAL_MAX_URL))
@deal.post(lambda result: isinstance(result, bool))
def is_pdf_url(url):
    """Check for .pdf in the URL path safely."""
    if type(url) is not str:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.path or "").lower().endswith(".pdf")
    except (ValueError, TypeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Filesystem / document file: URLs (UNO-free; not HTTP endpoints)
# ---------------------------------------------------------------------------


def path_to_file_url(path: str) -> str:
    """Build a document / ``loadComponentFromURL`` / GraphicProvider-style ``file:`` URL.

    Normalize with ``os.path.abspath`` then ``Path.as_uri()`` so Unix yields
    ``file:///…`` (three slashes). ``urljoin('file:', …)`` wrongly produces
    ``file:/…``. Not an LLM endpoint helper; no ``@deal`` (those contracts are
    for HTTP strings). UNO-free so embeddings extract can import this module.
    """
    return Path(os.path.abspath(path)).as_uri()