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

log = logging.getLogger("writeragent.mcp_client")

MAX_BODY_BYTES = 10 * 1024 * 1024
# An SSE event ends at a blank line: two consecutive line terminators (\n\n, \r\r, \r\n\r\n, etc.).
# The lookahead keeps a plain CRLF line ending from backtracking into a \r + \n boundary.
SSE_BOUNDARY_RE = re.compile(rb"(?:\r\n|\r(?!\n)|\n){2}")
SSE_BOUNDARY_CARRY = 3  # longest boundary ("\r\n\r\n") minus one byte
MAX_LIST_PAGES = 50
HTTP_ERROR_BODY_CHARS = 300
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
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


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
    """Apply ``strip_unicode_tags`` to every string in a nested dict/list/tuple structure."""
    if isinstance(value, str):
        return strip_unicode_tags(value)
    if isinstance(value, dict):
        return {
            strip_unicode_tags_deep(k) if isinstance(k, str) else k: strip_unicode_tags_deep(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [strip_unicode_tags_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_unicode_tags_deep(v) for v in value)
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


def safe_numeric(
    value: Any,
    default: float,
    coerce: Callable[[Any], float] = int,
    minimum: float = 1,
) -> float:
    """Coerce a config value to a number clamped to ``minimum``, or ``default`` on failure/non-finite."""
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_boolish(value: Any, default: bool = True) -> bool:
    """Parse a bool-like config value with safe fallback."""
    if value is None:
        return default
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    log.warning("MCP config expected a boolean-ish value, got %r; using default=%s", value, default)
    return default


def server_enabled(entry: dict[str, Any]) -> bool:
    """Whether an ``mcp_client.servers`` entry is enabled (absent or null means on)."""
    return parse_boolish(entry.get("enabled", True), default=True)


def normalize_server_trust(value: Any) -> str:
    """Map config ``trust`` to ``full`` or ``untrusted`` (None/unknown fail closed to untrusted)."""
    if value is None:
        return TRUST_UNTRUSTED
    text = str(value).strip().lower()
    if text in (TRUST_FULL, TRUST_UNTRUSTED):
        return text
    log.warning(
        "MCP trust: unrecognized trust value %r — treating as 'untrusted' (valid values: full, untrusted)",
        value,
    )
    return TRUST_UNTRUSTED


def annotation_read_only_hint(spec: dict[str, Any]) -> bool:
    """True only when ``spec['annotations']`` carries an exact ``True`` read-only hint."""
    annotations = spec.get("annotations")
    if not isinstance(annotations, dict):
        return False
    hint = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    return hint is True


def strip_bearer_prefix(token: str) -> str:
    """Strip a leading ``Bearer `` prefix from a pasted token."""
    if not isinstance(token, str):
        return token
    stripped = token.strip()
    if stripped[:7].lower() == "bearer ":
        return stripped[7:].strip()
    return stripped


def warn_hidden_whitespace(server_name: str, entry: dict[str, Any]) -> list[str]:
    """Warn about string values with leading/trailing whitespace without logging secrets."""
    flagged: list[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str) and value != value.strip():
            flagged.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _walk(v, f"{path}[{i}]")

    _walk(entry, "")
    for key_path in flagged:
        log.warning(
            "MCP server '%s': config value '%s' has hidden leading or trailing whitespace — this often "
            "causes authentication or connection failures. Check for stray spaces/newlines in settings.",
            server_name,
            key_path,
        )
    return flagged


def validate_remote_url(server_name: str, url: Any) -> str:
    """Return the stripped URL if valid http(s) with a host; else raise ValueError."""
    def _bad(detail: str) -> ValueError:
        return ValueError(f"Invalid MCP URL for '{server_name}': {detail}")

    if not isinstance(url, str):
        raise _bad(f"expected a string, got {type(url).__name__}")
    stripped = url.strip()
    if not stripped:
        raise _bad("empty url")
    try:
        parsed = urlparse(stripped)
    except Exception as exc:
        raise _bad(f"{stripped!r} ({exc})") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _bad(f"scheme must be http or https, got {parsed.scheme!r} ({stripped!r})")
    if not parsed.netloc:
        raise _bad(f"missing host ({stripped!r})")
    if not parsed.hostname:
        raise _bad(f"missing hostname ({stripped!r})")
    return stripped


def unresolved_env_refs(entry: dict[str, Any]) -> list[str]:
    """Return sorted ``${VAR}`` / ``${env:VAR}`` references found in ``url`` or ``headers`` values."""
    headers = entry.get("headers")
    header_vals = list(headers.values()) if isinstance(headers, dict) else []
    values = [entry.get("url") or "", *header_vals]
    return sorted({m.group(1) for value in values for m in _ENV_VAR_PATTERN.finditer(str(value))})


def normalize_name_filter(value: Any, label: str) -> set[str]:
    """Normalize include/exclude config to a set of exact names or fnmatch globs."""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    log.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """True if ``tool_name`` matches an exact name or case-sensitive fnmatch glob in ``patterns``."""
    if not patterns:
        return False
    if tool_name in patterns:
        return True
    return any(fnmatch.fnmatchcase(tool_name, p) for p in patterns if "*" in p or "?" in p or "[" in p)


def make_tool_filter(name: str, entry: dict[str, Any]) -> Callable[[str], bool]:
    """Build an include/exclude predicate for a server's tool names (include wins over exclude)."""
    tools_filter = entry.get("tools")
    if not isinstance(tools_filter, dict):
        tools_filter = {}
    include_raw = tools_filter.get("include")
    include_set = normalize_name_filter(include_raw, f"mcp_client.servers.{name}.tools.include")
    exclude_set = normalize_name_filter(tools_filter.get("exclude"), f"mcp_client.servers.{name}.tools.exclude")
    if isinstance(include_raw, (str, list, tuple, set)):
        return lambda tool_name: matches_name_filter(tool_name, include_set)
    return lambda tool_name: not (exclude_set and matches_name_filter(tool_name, exclude_set))


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
    """Format call arguments as bounded, indented JSON with invisible/control chars escaped."""
    rendered = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    window = 4 * _APPROVAL_ARGUMENTS_MAX_CHARS
    cut = len(rendered) > window
    rendered = escape_invisible(rendered[:window])
    if cut or len(rendered) > _APPROVAL_ARGUMENTS_MAX_CHARS:
        keep = _APPROVAL_ARGUMENTS_MAX_CHARS - len(_APPROVAL_ARGUMENTS_TRUNCATION_MARKER)
        rendered = rendered[:keep].rstrip() + _APPROVAL_ARGUMENTS_TRUNCATION_MARKER
    return rendered


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
