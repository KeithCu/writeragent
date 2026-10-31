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

from plugin.framework.deal_shim import deal


def matches_extension_dispatch_url(url: Any, extension_id: str) -> bool:
    """Return True when *url* is an extension menu/protocol dispatch URL."""
    prefix = extension_id + ":"
    return str(getattr(url, "Protocol", None) or "").startswith(prefix) or str(getattr(url, "Complete", None) or "").startswith(prefix)


def dispatch_command_from_url(url: Any) -> str:
    """Extract handler command from a LibreOffice dispatch URL.

    Shared extractor for LibrePy and WriterAgent dispatch URLs.
    Do not append ``Query`` here.

    LO usually sets ``Path`` (e.g. ``main.settings``). Some dispatch paths only
    populate ``Complete`` (``org.extension.librepy:main.settings``); derive Path
    from that so menu handlers still run.
    """
    path = str(getattr(url, "Path", None) or "").strip().lstrip("/")
    if path:
        return path
    before, colon, after = str(getattr(url, "Complete", None) or "").strip().partition(":")
    return after.lstrip("/") if colon else before


def host_matches(host: str, *domains: str) -> bool:
    """True when host equals any domain or is a subdomain of it."""
    h = (host or "").lower()
    return any(h == domain or h.endswith("." + domain) for domain in domains)


@deal.post(lambda result: isinstance(result, str) and result.startswith("/"))
def get_api_version_suffix(url: Any, is_openwebui: bool = False) -> str:
    """Return the API version suffix (e.g. '/v1', '/v4', '/api/paas/v4') for a given endpoint URL."""
    if is_openwebui:
        return "/api"
    host = get_url_hostname(url)
    # Z.ai: bare host uses general OpenAI base (/api/paas/v4); deeper paths append /v4 only.
    if host_matches(host, "z.ai"):
        if not get_url_path(url).rstrip("/"):
            return "/api/paas/v4"
        return "/v4"
    # Google Gemini: OpenAI-compatible API base is /v1beta/openai
    if host_matches(host, "generativelanguage.googleapis.com"):
        return "/v1beta/openai"
    return "/v1"


@deal.post(lambda result: isinstance(result, str))
@deal.ensure(lambda url, is_openwebui=False, result="": bool(isinstance(url, str) and url.strip()) or result == "")
def normalize_endpoint_url(url: Any, is_openwebui: bool = False) -> str:
    """Clean up endpoint URL: strip whitespace, trailing slashes, and domain-specific version suffixes."""
    if type(url) is not str or not url.strip():
        return ""
    url = url.strip().rstrip("/")
    # One strip, longest suffix first. Open WebUI must take /api/v1 in one
    # pass (half-stripping to /api, then re-appending /api, yields /api/api/...).
    # Z.ai legacy presets stored https://api.z.ai/v4 before the general base
    # was /api/paas/v4. That /v4 is only a fallback after the live suffix.
    if is_openwebui:
        suffixes = ("/api/v1", "/api", "/v1")
    else:
        host = get_url_hostname(url)
        extra = ("/v1beta",) if host_matches(host, "generativelanguage.googleapis.com") else ("/v4",) if host_matches(host, "z.ai") else ()
        suffixes = (get_api_version_suffix(url), *extra, "/v1")
    for suffix in suffixes:
        if url.lower().endswith(suffix):
            return url[: -len(suffix)]
    return url


def _parse(url: Any) -> urllib.parse.ParseResult:
    try:
        return urllib.parse.urlparse(url if type(url) is str else "")
    except ValueError:
        return urllib.parse.urlparse("")


@deal.post(lambda result: isinstance(result, str))
def get_url_hostname(url: Any) -> str:
    """Return hostname from URL safely."""
    return _parse(url).hostname or ""


@deal.post(lambda result: isinstance(result, str))
def get_url_domain(url: Any) -> str:
    """Return 'example.com' from 'https://api.example.com/v1'."""
    return ".".join(get_url_hostname(url).split(".")[-2:])


@deal.post(lambda result: isinstance(result, str))
def get_url_path(url: Any) -> str:
    """Return path from URL safely."""
    return _parse(url).path


@deal.post(lambda result: isinstance(result, dict))
def get_url_query_dict(url: Any) -> dict[str, list[str]]:
    """Return query parameters as dict (values are lists)."""
    return urllib.parse.parse_qs(_parse(url).query)


@deal.post(lambda result: isinstance(result, str))
def get_url_path_and_query(url: Any) -> str:
    """Return path + query string from URL."""
    parsed = _parse(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


@deal.post(lambda result: isinstance(result, bool))
def is_pdf_url(url: Any) -> bool:
    """Check for .pdf in the URL path safely."""
    return _parse(url).path.lower().endswith(".pdf")


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


def normalize_file_url(url: str) -> str:
    """Repair ``file:/path`` URLs from the old ``urljoin('file:', ...)`` form.

    That join wrongly yields ``file:/home/...`` (two slashes).
    ``loadComponentFromURL`` and ``fileUrlToSystemPath`` need ``file:///home/...``.
    Shared by ``get_document_path`` and document-research URL→path.
    Sandbox / session_manager keep their own stdlib copies (no UNO, Windows).
    """
    raw = str(url).strip()
    if raw.startswith("file:/") and not raw.startswith("file://"):
        return "file://" + raw[len("file:") :]
    return raw

