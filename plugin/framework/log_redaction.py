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
from typing import TYPE_CHECKING, TypedDict

from plugin.framework.process_global import process_global

if TYPE_CHECKING:
    from collections.abc import Iterable

LOG_REDACT_SECRET_PLACEHOLDER = "<redacted>"
# UUIDs, hex keys, base64 and nanoid links are longer than this.
_OPAQUE_SEGMENT_MIN_CHARS = 16
# A model id follows these segments and errors must show it.
_MODEL_ID_PARENTS = frozenset({"models", "deployments"})
# Global log redaction uses a higher floor than per-request redact_secrets so short
# placeholder keys (e.g. "ollama", "test") do not blank out hosts, models, or paths.
_MIN_KNOWN_SECRET_LEN = 8


class _KnownSecretsState(TypedDict):
    lock: threading.Lock
    secrets: set[str]


def _known_secrets_state() -> _KnownSecretsState:
    return process_global("known_secrets_state", lambda: _KnownSecretsState(lock=threading.Lock(), secrets=set()))

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


def is_credential_name(name: str) -> bool:
    """True when an HTTP header called *name* carries a credential.

    Also the base of ``is_credential_query_name``. Broader than ``is_secret_key``, which also
    judges tool arguments, where names such as ``sort_key`` are data. ``max_tokens`` and
    ``output_modalities`` are not credentials.
    """
    return is_secret_key(name) or name.lower().endswith(("key", "token"))


def is_credential_query_name(name: str) -> bool:
    """True when a URL query parameter called *name* carries a credential.

    Azure Functions passes its key as ``code`` and signed URLs use ``sig``, ``signature`` or
    ``X-Amz-Signature``. Headers with those names are ordinary data.
    """
    low = name.lower()
    return is_credential_name(name) or low in ("code", "sig") or low.endswith("signature")


def redact_secrets(
    text: str,
    secrets: Iterable[str] | None,
    *,
    min_len: int = 4,
    word_boundaries: bool = False,
) -> str:
    """Replace credential strings with ``<redacted>``. Longer values first."""
    if not text or not secrets:
        return text
    # A short key would punch holes through ordinary text ("k", "1", "test").
    ordered = sorted({secret for secret in secrets if secret and len(secret) >= min_len}, key=len, reverse=True)
    for secret in ordered:
        if word_boundaries:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", LOG_REDACT_SECRET_PLACEHOLDER, text)
        elif secret in text:
            text = text.replace(secret, LOG_REDACT_SECRET_PLACEHOLDER)
    return text


def opaque_segments(segments: list[str]) -> list[str]:
    """Path segments that look like opaque credentials, as written and percent-decoded."""
    secrets: list[str] = []
    for segment in segments:
        decoded = urllib.parse.unquote(segment)
        # All-lowercase segments without digits are words ("model-context-protocol").
        if len(decoded) >= _OPAQUE_SEGMENT_MIN_CHARS and any(ch.isdigit() or ch.isupper() for ch in decoded):
            secrets.extend((segment, decoded))
    return secrets


def request_path_secrets(path: str) -> list[str]:
    """Opaque credential segments in *path* before any model-id parent segment."""
    segments = path.split("/")
    for idx, segment in enumerate(segments):
        if segment in _MODEL_ID_PARENTS:
            segments = segments[:idx]
            break
    return opaque_segments(segments)


def public_path(path: str) -> str:
    """Return *path* with opaque credential segments replaced by ``<redacted>``."""
    return redact_secrets(path, request_path_secrets(path))


def bracket_ipv6_host(host: str) -> str:
    """Put brackets back around an IPv6 literal so the origin can be parsed again.

    ``ParseResult.hostname`` strips the brackets RFC 3986 requires. Rebuilding
    ``http://[::1]:11434`` as ``http://::1:11434`` makes the next parse read
    ``:1:11434`` as the port, and ``_explicit_port`` raises ``INVALID_URL``.
    A colon in the host is an IPv6 literal; wrap it once.
    """
    if ":" not in host or host.startswith("["):
        return host
    return f"[{host}]"


def without_userinfo(rest: str, tail: str) -> str:
    """What follows the last "@" of *rest*, or "" when the cut-off query or fragment *tail* has an "@".

    For text that does not parse as an absolute URL: a raw "/" may be part of the password (keep only
    what follows the last "@"), and a raw "?" or "#" in the password hides the "@" in *tail*.
    """
    return "" if "@" in tail else rest.rpartition("@")[2]


# Match any scheme://... up to whitespace or backslash (JSON-escaped \n).
# Apostrophes and closing parens are valid in userinfo/paths/queries and are not
# terminators here; over-masking a log line is acceptable, under-masking is not.
# Raw spaces inside userinfo split tokens in unstructured prose and are not spanned.
_SCHEME_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^\s\\/?#]+)@", re.IGNORECASE)
_SCHEMELESS_USERINFO_RE = re.compile(r"(?<![A-Za-z0-9+.-])(?!mailto:)([^\s\\/?#\"':@]+:[^\s\\/?#]+)@(?=[^\s\\/?#])")
_SCHEME_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\\<>]+", re.IGNORECASE)
_QUERY_CREDENTIAL_RE = re.compile(r"([?&])([a-zA-Z0-9_.-]+)=([^&\s\"\\<>#]+)")


def _mask_scheme_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    # Strip trailing sentence/quote delimiters so wrapped URLs keep their closing punctuation.
    stripped = raw.rstrip(".,;:!?'\")]}")
    suffix = raw[len(stripped) :]
    if not stripped:
        return raw
    try:
        parsed = urllib.parse.urlparse(stripped)
        host = bracket_ipv6_host(parsed.hostname or "")
        port = parsed.port
        port_str = f":{port}" if port else ""
        scheme = parsed.scheme or "http"
        path = public_path(parsed.path or "")
        return f"{scheme}://{host}{port_str}{path}{suffix}"
    except ValueError:
        base, *tail = re.split(r"[?#]", stripped, maxsplit=1)
        scheme, sep, rest = base.partition("://")
        rest = without_userinfo(rest, tail[0] if tail else "")
        host_part, slash, path_part = rest.partition("/")
        masked_path = public_path(path_part) if slash else ""
        path_str = f"/{masked_path}" if slash else ""
        return f"{scheme}{sep}{host_part}{path_str}{suffix}"


def _mask_query_credential(match: re.Match[str]) -> str:
    if is_credential_query_name(match.group(2)):
        return f"{match.group(1)}{match.group(2)}={LOG_REDACT_SECRET_PLACEHOLDER}"
    return match.group(0)


def redact_url_credentials_in_text(text: str) -> str:
    """Strip userinfo, query, fragment, and opaque path segments from URLs, plus credential query params."""
    if not text:
        return text
    if "://" in text:
        text = _SCHEME_USERINFO_RE.sub(r"\1", text)
    if "@" in text:
        text = _SCHEMELESS_USERINFO_RE.sub("", text)
    if "://" in text:
        text = _SCHEME_URL_RE.sub(_mask_scheme_url, text)
    if "?" in text or "&" in text:
        text = _QUERY_CREDENTIAL_RE.sub(_mask_query_credential, text)
    return text


def register_known_secret(secret: str | None) -> None:
    """Register a runtime secret value so RedactingFormatter masks it in every log record."""
    if not secret or len(secret) < _MIN_KNOWN_SECRET_LEN or secret == LOG_REDACT_SECRET_PLACEHOLDER or secret.isspace():
        return
    stripped = secret.strip()
    state = _known_secrets_state()
    with state["lock"]:
        state["secrets"].add(secret)
        if stripped != secret and len(stripped) >= _MIN_KNOWN_SECRET_LEN and stripped != LOG_REDACT_SECRET_PLACEHOLDER:
            state["secrets"].add(stripped)


def clear_known_secrets() -> None:
    """Clear registered runtime secrets (for test isolation)."""
    state = _known_secrets_state()
    with state["lock"]:
        state["secrets"].clear()


def redact_known_secrets_in_text(text: str) -> str:
    """Replace any registered known secret values in *text* with ``<redacted>``."""
    if not text:
        return text
    state = _known_secrets_state()
    with state["lock"]:
        if not state["secrets"]:
            return text
        snapshot = list(state["secrets"])
    return redact_secrets(text, snapshot, min_len=_MIN_KNOWN_SECRET_LEN, word_boundaries=True)


class RedactingFormatter(logging.Formatter):
    """Masks secret key/value pairs, URL credentials/queries, and known secrets in the finished line."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = redact_secret_pairs_in_text(text)
        text = redact_url_credentials_in_text(text)
        return redact_known_secrets_in_text(text)
