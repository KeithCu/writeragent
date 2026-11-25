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

LOG_REDACT_SECRET_PLACEHOLDER = "<redacted>"

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


class RedactingFormatter(logging.Formatter):
    """Masks secret key/value pairs in the finished line, so no caller has to."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secret_pairs_in_text(super().format(record))
