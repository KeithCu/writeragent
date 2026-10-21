# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON wire parsing and language detection result handlers for AI grammar proofreader.

This module encapsulates all JSON parsing and regex repair logic, extracting it from
the main locale policy to ensure cleaner separation of concerns.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping, cast

from plugin.framework.json_utils import repair_json_object, safe_json_loads

_log = logging.getLogger("writeragent.grammar")

# Storage key mapping for error objects to reduce JSON footprint.
_ERROR_KEY_MAP = {
    "n_error_start": "s",
    "n_error_length": "l",
    "suggestions": "g",
    "short_comment": "c",
    "full_comment": "f",
    "rule_identifier": "r",
}
_REV_ERROR_KEY_MAP = {v: k for k, v in _ERROR_KEY_MAP.items()}


def compress_error(err: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the error dict with shortened keys for storage."""
    out = {}
    for k, v in err.items():
        short = _ERROR_KEY_MAP.get(k)
        if short:
            out[short] = v
        else:
            out[k] = v
    return out


def decompress_error(err: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the error dict with long keys restored for runtime."""
    out = {}
    for k, v in err.items():
        long = _REV_ERROR_KEY_MAP.get(k)
        if long:
            out[long] = v
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# LLM JSON wire format
# ---------------------------------------------------------------------------

# Matches the closing portion of JSON in case of extra leading text/markdown.
GRAMMAR_JSON_TAIL_RE = re.compile(r"\{[\s\S]*\}\s*$")


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return None


def _coerce_json_object(content: str) -> Mapping[str, Any] | None:
    """Extract and parse a JSON object from assistant response text, with json_repair fallback."""
    if not content or not content.strip():
        return None
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if _as_mapping(data) is None:
        try:
            _log.info("[grammar] JSON parse failed; attempting json_repair")
            data = repair_json_object(text)
        except Exception as e:
            _log.warning("[grammar] json_repair failed: %s", e)
            return None
    return _as_mapping(data)


def _parse_error_item(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map one LLM error object to ``wrong``/``correct``/``type``/``reason``, or None if incomplete."""
    wrong = row.get("wrong")
    correct = row.get("correct")
    if wrong is None or correct is None:
        return None
    return {
        "wrong": str(wrong),
        "correct": str(correct),
        "type": str(row.get("type", "grammar")),
        "reason": str(row.get("reason", "")),
    }


def _errors_of(mapping: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = mapping.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        mapped = _as_mapping(item)
        if mapped is None:
            continue
        parsed = _parse_error_item(mapped)
        if parsed is not None:
            out.append(parsed)
    return out


def _detected_language(mapping: Mapping[str, Any]) -> str | None:
    lang = mapping.get("detected_language_bcp47")
    return lang if isinstance(lang, str) else None


def _map_payload(
    content: str,
    parse_one: Callable[[Mapping[str, Any]], Any],
    *,
    batch: bool,
    missing_slot: Any = None,
    if_root_missing: Any = None,
) -> Any:
    root = _coerce_json_object(content)
    if root is None:
        return [] if batch else if_root_missing
    if not batch:
        return parse_one(root)
    results = root.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for res in results:
        mapped = _as_mapping(res)
        out.append(missing_slot if mapped is None else parse_one(mapped))
    return out


def parse_grammar_json(content: str) -> list[dict[str, Any]]:
    """Parse assistant message into a list of error dicts (wrong, correct, type, reason)."""
    return _map_payload(content, _errors_of, batch=False, if_root_missing=[])


def parse_grammar_batch_json(content: str) -> list[list[dict[str, Any]]]:
    """Parse assistant message into a list of lists of error dicts."""
    return _map_payload(content, _errors_of, batch=True, missing_slot=[])


def parse_language_detect_json(content: str) -> str | None:
    """Parse assistant message to extract the detected language string."""
    return _map_payload(content, _detected_language, batch=False, if_root_missing=None)


def parse_language_detect_batch_json(content: str) -> list[str | None]:
    """Parse assistant message into a list of detected languages."""
    return _map_payload(content, _detected_language, batch=True, missing_slot=None)


