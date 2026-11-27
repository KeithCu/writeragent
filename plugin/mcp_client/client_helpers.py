# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Portions ported from Hermes Agent:
# MIT License
# Copyright (c) 2025 Nous Research
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# strip_unicode_tags ported from block/goose (Apache-2.0):
# Copyright 2024 Block, Inc.
#
# is_reserved_meta_key ported from MoonshotAI/kimi-code (MIT):
# Copyright (c) 2026 Moonshot AI
"""Pure helpers for the external MCP client (sanitization, caps, filters, config coercion)."""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import re
import unicodedata
from typing import Any, Callable
from urllib.parse import urlparse

from plugin.framework.config_schema import as_bool
from plugin.framework.logging import redact_sensitive_payload_for_log

log = logging.getLogger("writeragent.mcp_client")

MAX_BODY_BYTES = 10 * 1024 * 1024
# An SSE event ends at a blank line: two consecutive line terminators (\n\n, \r\r, \r\n\r\n, etc.).
# The lookahead keeps a plain CRLF line ending from backtracking into a \r + \n boundary.
SSE_BOUNDARY_RE = re.compile(rb"(?:\r\n|\r(?!\n)|\n){2}")
SSE_BOUNDARY_CARRY = 3  # longest boundary ("\r\n\r\n") minus one byte
MAX_LIST_PAGES = 50
HTTP_ERROR_BODY_CHARS = 300
# Read well past HTTP_ERROR_BODY_CHARS so a long echoed credential is matched whole before the text is cut.
HTTP_ERROR_READ_BYTES = 16384
DEFAULT_TOOL_TIMEOUT_SEC = 300.0
DEFAULT_CONNECT_TIMEOUT_SEC = 60.0
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0
TRUST_FULL = "full"
TRUST_UNTRUSTED = "untrusted"
OUTCOME_UNCERTAIN_MSG = (
    "The connection to MCP server '{s}' was lost after the call was sent, so the outcome is "
    "UNKNOWN — the operation may or may not have taken effect server-side. It was NOT automatically retried to "
    "avoid a duplicate side effect. Verify whether the operation took effect (e.g. "
    "with a read-only tool) before re-invoking it."
)

_HEAD_RATIO = 0.4
_APPROVAL_ARGUMENTS_MAX_CHARS = 1200
_APPROVAL_ARGUMENTS_TRUNCATION_MARKER = "\n... [arguments truncated]"

_HAS_UNICODE_TAG = re.compile(r"[\U000E0000-\U000E007F]")
# Unicode TAG chars (U+E0000-U+E007F) are invisible to users but visible to LLM tokenizers.
# Emoji tag sequences (TR51: U+1F3F4 base + tag spec + U+E007F CANCEL TAG) are preserved.
_UNICODE_TAG_SUB_RE = re.compile(
    r"(\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F)"
    r"|[\U000E0000-\U000E007F]"
)
_BIDI_CONTROL_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_CREDENTIAL_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_-](?:\.?[A-Za-z0-9_-]){0,254}|Bearer\s+\S+"
    r"|(?:token|key|API_KEY|password|secret)=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def strip_unicode_tags(text: str) -> str:
    """Remove invisible Unicode TAG chars while preserving valid emoji tag sequences."""
    if not text or not _HAS_UNICODE_TAG.search(text):
        return text
    return _UNICODE_TAG_SUB_RE.sub(lambda m: m.group(1) or "", text)


def strip_tool_metadata_text(text: str) -> str:
    """Strip invisible Unicode TAG chars plus bidi override/isolate controls for MCP tool metadata."""
    text = strip_unicode_tags(text)
    if not text or not _BIDI_CONTROL_RE.search(text):
        return text
    return _BIDI_CONTROL_RE.sub("", text)


def strip_unicode_tags_deep(value: Any) -> Any:
    """Apply ``strip_unicode_tags`` to every string in a nested dict/list structure."""
    if isinstance(value, str):
        return strip_unicode_tags(value)
    if isinstance(value, dict):
        return {strip_unicode_tags(k): strip_unicode_tags_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_unicode_tags_deep(v) for v in value]
    return value


def truncate_head_tail(text: str, max_chars: int, *, label: str = "OUTPUT") -> str:
    """Return ``text`` unchanged if it fits ``max_chars``; else 40% head + notice + 60% tail."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * _HEAD_RATIO)
    tail = max_chars - head
    omitted = len(text) - head - tail
    notice = f"\n\n... [{label} TRUNCATED - {omitted:,} chars omitted out of {len(text):,} total] ...\n\n"
    return text[:head] + notice + text[-tail:]


def sanitize_error(text: str) -> str:
    """Replace credential-like patterns with ``[REDACTED]`` and strip Unicode TAG chars."""
    return strip_unicode_tags(_CREDENTIAL_PATTERN.sub("[REDACTED]", text))


def secret_spellings(secret: str) -> list[str]:
    """*secret* as written and as JSON or repr() escapes it: a server may echo it inside a JSON error body."""
    json_escaped = json.dumps(secret)[1:-1]
    return [
        secret,
        json_escaped,
        json_escaped.replace("/", r"\/"),
        json.dumps(secret, ensure_ascii=False)[1:-1],
        repr(secret)[1:-1],
    ]


def timeout_seconds(value: Any, default: float) -> float:
    """A config value in seconds as a finite float of at least 1, or *default* when missing or unusable."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(result, 1.0) if math.isfinite(result) else default


def server_enabled(entry: dict[str, Any]) -> bool:
    """Whether an ``mcp_client.servers`` entry is enabled (absent or null means on)."""
    value = entry.get("enabled")
    return value is None or as_bool(value)


def server_trusted(entry: dict[str, Any]) -> bool:
    """True only for ``"trust": "full"``; anything else asks before every call."""
    return entry.get("trust") == TRUST_FULL


def annotation_read_only_hint(spec: dict[str, Any]) -> bool:
    """True only when ``spec['annotations']`` carries an exact ``True`` read-only hint."""
    annotations = spec.get("annotations")
    if not isinstance(annotations, dict):
        return False
    hint = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    return hint is True


def strip_bearer_prefix(token: str) -> str:
    """Strip a leading ``Bearer `` prefix from a pasted token."""
    stripped = token.strip()
    if stripped[:7].lower() == "bearer ":
        return stripped[7:].strip()
    return stripped


def validate_remote_url(server_name: str, url: Any) -> str:
    """Return the stripped URL if valid http(s) with a host; else raise ValueError."""
    def _bad(detail: str) -> ValueError:
        return ValueError(f"Invalid MCP URL for '{server_name}': {detail}")

    if not isinstance(url, str):
        raise _bad(f"expected a string, got {type(url).__name__}")
    stripped = url.strip()
    # The URL is not quoted in these errors because it may hold a token or userinfo password.
    try:
        parsed = urlparse(stripped)
    except ValueError as exc:
        raise _bad("it cannot be parsed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _bad("scheme must be http or https")
    if not parsed.hostname:
        raise _bad("missing host")
    return stripped


# RFC 9110 section 5.6.2 token. http.client also accepts spaces inside a name and sends it malformed.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def bad_header_position(entry: dict[str, Any]) -> int | None:
    """1-based position of the first invalid header name in *entry*; None when all are valid."""
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        return None
    for idx, name in enumerate(headers, 1):
        if not _HEADER_NAME_RE.fullmatch(str(name)):
            return idx
    return None


def unresolved_env_refs(entry: dict[str, Any]) -> list[str]:
    """Return sorted ``${VAR}`` / ``${env:VAR}`` references found in ``url`` or ``headers`` values."""
    headers = entry.get("headers")
    header_vals = list(headers.values()) if isinstance(headers, dict) else []
    values = [entry.get("url") or "", *header_vals]
    return sorted({m.group(1) for value in values for m in _ENV_VAR_PATTERN.finditer(str(value))})


def normalize_name_filter(value: Any, label: str) -> set[str] | None:
    """Include/exclude config as exact names or fnmatch globs; None when unset or invalid."""
    if value is None:
        return None
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    log.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return None


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """True if ``tool_name`` equals a pattern or matches it as a case-sensitive fnmatch glob."""
    return tool_name in patterns or any(fnmatch.fnmatchcase(tool_name, p) for p in patterns)


def make_tool_filter(name: str, entry: dict[str, Any]) -> Callable[[str], bool]:
    """Build an include/exclude predicate for a server's tool names (a valid include wins over exclude)."""
    tools_filter = entry.get("tools")
    if not isinstance(tools_filter, dict):
        tools_filter = {}
    label = f"mcp_client.servers.{name}.tools"
    include = normalize_name_filter(tools_filter.get("include"), f"{label}.include")
    exclude = normalize_name_filter(tools_filter.get("exclude"), f"{label}.exclude") or set()
    if include is not None:
        return lambda tool_name: matches_name_filter(tool_name, include)
    return lambda tool_name: not matches_name_filter(tool_name, exclude)


def is_reserved_meta_key(key: str) -> bool:
    """True if an MCP ``_meta`` key uses a protocol-reserved ``modelcontextprotocol`` or ``mcp`` prefix."""
    slash = key.find("/")
    if slash <= 0:
        return False
    labels = key[:slash].split(".")
    return any(label in ("modelcontextprotocol", "mcp") and i < len(labels) - 1 for i, label in enumerate(labels))


def strip_reserved_meta_keys(meta: Any) -> dict[str, Any] | None:
    """Drop protocol-reserved keys from ``_meta``; None if nothing remains or input is not a dict."""
    if not isinstance(meta, dict):
        return None
    out = {k: v for k, v in meta.items() if isinstance(k, str) and not is_reserved_meta_key(k)}
    return out or None


def content_dual_emits_structured(result: dict[str, Any], structured: Any) -> bool:
    """True when some text block in ``result`` is ``structured`` serialized as JSON."""
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not text or not isinstance(text, str):
            continue
        try:
            if json.loads(text) == structured:
                return True
        except (TypeError, ValueError):
            continue
    return False


def escape_invisible(text: str) -> str:
    """Escape Cc/Cf control and format characters (except ``\\n``) as valid JSON escapes."""
    return "".join(
        json.dumps(ch)[1:-1] if ch != "\n" and unicodedata.category(ch) in ("Cc", "Cf") else ch
        for ch in text
    )


def format_approval_arguments(args: dict[str, Any]) -> str:
    """Format call arguments as bounded, indented JSON with secrets masked and control chars escaped."""
    rendered = json.dumps(redact_sensitive_payload_for_log(args), ensure_ascii=False, indent=2, default=str)
    window = 4 * _APPROVAL_ARGUMENTS_MAX_CHARS
    cut = len(rendered) > window
    rendered = escape_invisible(rendered[:window])
    if cut or len(rendered) > _APPROVAL_ARGUMENTS_MAX_CHARS:
        keep = _APPROVAL_ARGUMENTS_MAX_CHARS - len(_APPROVAL_ARGUMENTS_TRUNCATION_MARKER)
        rendered = rendered[:keep].rstrip() + _APPROVAL_ARGUMENTS_TRUNCATION_MARKER
    return rendered


def restore_masked(edited: Any, original: Any, shown: Any) -> Any:
    """Restore real secret values where the user left the masked placeholder unchanged."""
    if edited == shown:
        return original
    if isinstance(edited, dict) and isinstance(original, dict) and isinstance(shown, dict):
        return {
            k: restore_masked(v, original[k], shown[k]) if k in original and k in shown else v
            for k, v in edited.items()
        }
    if isinstance(edited, list) and isinstance(original, list) and isinstance(shown, list) and len(edited) == len(original) == len(shown):
        return [restore_masked(e, o, s) for e, o, s in zip(edited, original, shown)]
    return edited


def names_unknown_tool(tool: str, text: str) -> bool:
    """True when *text* is a server saying it has no tool called *tool*.

    The MCP spec answers an unknown name with JSON-RPC -32602 "Unknown tool: <name>".
    The Python SDK returns the same words as an isError result
    (mcp/server/mcpserver/tools/tool_manager.py ToolManager.call_tool), and the TypeScript
    SDK returns "Tool <name> not found" (sdk/dist/esm/server/mcp.js tools/call handler).
    The tool's own name must appear, so a tool's own "file not found" error never matches.
    """
    name = re.escape(tool)
    pattern = rf"unknown tool:?\s*['\"`]?{name}(?![\w.-])|\btool\s+['\"`]?{name}['\"`]?\s+not found"
    return re.search(pattern, text, re.IGNORECASE) is not None
