# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Word-bounded secret key matching (_is_word_start, _is_word_end, is_secret_key)
# adapted from Hermes Agent (agent/redact.py), MIT License.
"""Masks secret key/value pairs and URLs in log text.

Stdlib only, because the venv worker imports it and must not load
``plugin.framework.config`` or ``uno``.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse

LOG_REDACT_SECRET_PLACEHOLDER = "<redacted>"
_OPAQUE_SEGMENT_MIN_CHARS = 16
_MODEL_ID_PARENTS = frozenset({"models", "deployments"})
_MIN_KNOWN_SECRET_LEN = 4

_KNOWN_SECRETS_LOCK = threading.Lock()
_KNOWN_SECRETS: set[str] = set()

_SECRET_KEY_WORD_RE = re.compile(
    r"api[_.\-]?key|token|secret|password|passwd|passphrase|private[_.\-]?key|authorization|bearer|credentials?|cookie",
    re.IGNORECASE,
)


def _is_word_start(key: str, idx: int) -> bool:
    if idx == 0:
        return True
    prev = key[idx - 1]
    if not prev.isalnum():
        return True
    if prev.islower() and key[idx].isupper():
        return True
    return prev.isupper() and key[idx].isupper() and idx + 1 < len(key) and key[idx + 1].islower()


def _is_word_end(key: str, end: int) -> bool:
    if end == len(key):
        return True
    nxt = key[end]
    if not nxt.isalnum():
        return True
    if key[end - 1].islower() and nxt.isupper():
        return True
    return key[end - 1].isupper() and nxt.isupper() and end + 1 < len(key) and key[end + 1].islower()


def is_secret_key(key: str) -> bool:
    """Return True if *key* contains a word-bounded secret identifier (e.g. token, apiKey, db_password)."""
    for match in _SECRET_KEY_WORD_RE.finditer(key):
        if _is_word_start(key, match.start()) and _is_word_end(key, match.end()):
            return True
    return False


# One quoted key, then ":" and either an opening quote (with its own escape run) or a number.
# Group 1 is the key's backslash run; group 4 is the value's opening backslash run, which can
# differ when a Python repr is inside JSON or JSON is inside a Python repr.
_SECRET_PAIR_RE = re.compile(r"""(?<!\\)(\\*)(["'])([^"'\\\n]{1,64})\1\2\s*:\s*(?:(\\*)(["'])|(-?\d[\d.eE+-]*))""")
_CLOSING_QUOTE_RES = {
    '"': re.compile(r'(?<!\\)(\\*)"'),
    "'": re.compile(r"(?<!\\)(\\*)'"),
}
_AFTER_VALUE_RE = re.compile(r"""\s*(?:[,}\])"']|\\n|$)""")


def redact_secret_pairs_in_text(text: str) -> str:
    """Mask the values of secret-named keys in JSON or Python-repr text, at any escape depth.

    Log lines hold tool arguments as text (raw argument strings, sub-agent replay
    messages, error text), where redact_sensitive_payload_for_log cannot see the keys.
    Separate from redact_sensitive_payload_for_log because approval cards round-trip
    that function's output through restore_masked, so a partly masked string could be
    sent to the remote server. An unterminated value is masked to the end of the text.
    """
    if not _SECRET_KEY_WORD_RE.search(text):
        return text
    out: list[str] = []
    pos = at = 0
    while (m := _SECRET_PAIR_RE.search(text, at)) is not None:
        escape = m[1]
        if not is_secret_key(m[3]):
            # Resume at the value: it may be a string that holds another, escaped pair.
            at = m.end(3) + len(escape) + 1
            continue
        if m[5] is None:
            quote = escape + m[2]
            out.append(text[pos : m.start(6)] + quote + LOG_REDACT_SECRET_PLACEHOLDER + quote)
            pos = at = m.end()
            continue
        # A closing quote has the value's own escape run plus whole escaped backslashes,
        # which come in multiples of 2 * (depth + 1), followed by valid post-value syntax.
        depth = len(m[4])
        stop = len(text)
        for c in _CLOSING_QUOTE_RES[m[5]].finditer(text, m.end()):
            if len(c[1]) % (2 * (depth + 1)) == depth and _AFTER_VALUE_RE.match(text, c.end()):
                stop = c.end() - 1 - depth
                break
        out.append(text[pos : m.end()] + LOG_REDACT_SECRET_PLACEHOLDER)
        pos = at = stop
    out.append(text[pos:])
    return "".join(out)


_URL_RE = re.compile(r"https?://\S+")


def redact_urls_in_text(text: str) -> str:
    """Mask every http(s) URL in *text*, host included.

    For messages that can hold the MCP tunnel URL: the server has no auth, so the host alone is the credential.
    """
    return _URL_RE.sub(LOG_REDACT_SECRET_PLACEHOLDER, text)


_HTTP_URL_RE = re.compile(r"https?://(?:\[[0-9a-fA-F:]+\]|[^\s\"'<>)\]])+")
_API_PATH_RE = re.compile(r"(?<![:\w])(/(?:s|v\d+(?:beta)?)/[^\s\"'<>?#]+)")
_QUERY_CREDENTIAL_RE = re.compile(r"([?&])([a-zA-Z0-9_.-]+)=([^&\s\"'<>#]+)")


def _is_credential_query_name(name: str) -> bool:
    low = name.lower()
    return is_secret_key(name) or low.endswith(("key", "token", "signature")) or low in ("code", "sig")


def _mask_opaque_path(path: str) -> str:
    if not path:
        return path
    segments = path.split("/")
    out: list[str] = []
    for idx, seg in enumerate(segments):
        if idx > 0 and seg in _MODEL_ID_PARENTS:
            out.extend(segments[idx:])
            break
        decoded = urllib.parse.unquote(seg)
        if len(decoded) >= _OPAQUE_SEGMENT_MIN_CHARS and any(ch.isdigit() or ch.isupper() for ch in decoded):
            out.append(LOG_REDACT_SECRET_PLACEHOLDER)
        else:
            out.append(seg)
    return "/".join(out)


def _mask_http_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    stripped = raw.rstrip(".,;:!?")
    suffix = raw[len(stripped) :]
    if not stripped:
        return raw
    try:
        parsed = urllib.parse.urlparse(stripped)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        port_str = f":{port}" if port else ""
        scheme = parsed.scheme or "http"
        path = _mask_opaque_path(parsed.path or "")
        return f"{scheme}://{host}{port_str}{path}{suffix}"
    except ValueError:
        base, *tail = re.split(r"[?#]", stripped, maxsplit=1)
        scheme, sep, rest = base.partition("://")
        rest = "" if (tail and "@" in tail[0]) else rest.rpartition("@")[2]
        host_part, slash, path_part = rest.partition("/")
        masked_path = _mask_opaque_path(path_part) if slash else ""
        path_str = f"/{masked_path}" if slash else ""
        return f"{scheme}{sep}{host_part}{path_str}{suffix}"


def _mask_query_credential(match: re.Match[str]) -> str:
    if _is_credential_query_name(match.group(2)):
        return f"{match.group(1)}{match.group(2)}={LOG_REDACT_SECRET_PLACEHOLDER}"
    return match.group(0)


def redact_url_credentials_in_text(text: str) -> str:
    """Strip userinfo, query, fragment, and opaque path segments from URLs, plus credential query params in paths."""
    if not text:
        return text
    if "://" in text:
        text = _HTTP_URL_RE.sub(_mask_http_url, text)
    if "/" in text:
        text = _API_PATH_RE.sub(lambda m: _mask_opaque_path(m.group(1)), text)
    if "?" in text or "&" in text:
        text = _QUERY_CREDENTIAL_RE.sub(_mask_query_credential, text)
    return text


def register_known_secret(secret: str | None) -> None:
    """Register a runtime secret value so RedactingFormatter masks it in every log record."""
    if not secret or len(secret) < _MIN_KNOWN_SECRET_LEN or secret == LOG_REDACT_SECRET_PLACEHOLDER or secret.isspace():
        return
    stripped = secret.strip()
    with _KNOWN_SECRETS_LOCK:
        _KNOWN_SECRETS.add(secret)
        if stripped != secret and len(stripped) >= _MIN_KNOWN_SECRET_LEN and stripped != LOG_REDACT_SECRET_PLACEHOLDER:
            _KNOWN_SECRETS.add(stripped)


def clear_known_secrets() -> None:
    """Clear registered runtime secrets (for test isolation)."""
    with _KNOWN_SECRETS_LOCK:
        _KNOWN_SECRETS.clear()


def redact_known_secrets_in_text(text: str) -> str:
    """Replace any registered known secret values in *text* with ``<redacted>``."""
    if not text:
        return text
    with _KNOWN_SECRETS_LOCK:
        if not _KNOWN_SECRETS:
            return text
        ordered = sorted(_KNOWN_SECRETS, key=len, reverse=True)
    for secret in ordered:
        if secret in text:
            text = text.replace(secret, LOG_REDACT_SECRET_PLACEHOLDER)
    return text


class RedactingFormatter(logging.Formatter):
    """Masks secret key/value pairs, URL credentials/queries, and known secrets in the finished line."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = redact_secret_pairs_in_text(text)
        text = redact_url_credentials_in_text(text)
        return redact_known_secrets_in_text(text)
