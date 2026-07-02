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
from typing import Any, Mapping, cast

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


def parse_grammar_json(content: str) -> list[dict[str, Any]]:
    """Parse assistant message into a list of error dicts (wrong, correct, type, reason)."""
    if not content or not content.strip():
        return []
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            _log.info("[grammar] parse_grammar_json: attempting json_repair")
            data = repair_json_object(text)
        except Exception as e:
            _log.warning("[grammar] parse_grammar_json: json_repair failed: %s", e)
            return []
    if not isinstance(data, Mapping):
        return []
    root = cast("Mapping[str, Any]", data)
    raw = root.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        row = cast("Mapping[str, Any]", item)
        wrong = row.get("wrong")
        correct = row.get("correct")
        if wrong is None or correct is None:
            continue
        out.append({
            "wrong": str(wrong),
            "correct": str(correct),
            "type": str(row.get("type", "grammar")),
            "reason": str(row.get("reason", "")),
        })
    return out


def parse_grammar_batch_json(content: str) -> list[list[dict[str, Any]]]:
    """Parse assistant message into a list of lists of error dicts."""
    if not content or not content.strip():
        return []
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            _log.info("[grammar] parse_grammar_batch_json: attempting json_repair")
            data = repair_json_object(text)
        except Exception as e:
            _log.warning("[grammar] parse_grammar_batch_json: json_repair failed: %s", e)
            return []
    if not isinstance(data, Mapping):
        return []
    root = cast("Mapping[str, Any]", data)
    results = root.get("results")
    if not isinstance(results, list):
        return []

    out: list[list[dict[str, Any]]] = []
    for res in results:
        if not isinstance(res, Mapping):
            out.append([])
            continue
        res_map = cast("Mapping[str, Any]", res)
        errors = res_map.get("errors")
        if not isinstance(errors, list):
            out.append([])
            continue
        sent_errors: list[dict[str, Any]] = []
        for item in errors:
            if not isinstance(item, Mapping):
                continue
            row = cast("Mapping[str, Any]", item)
            wrong = row.get("wrong")
            correct = row.get("correct")
            if wrong is None or correct is None:
                continue
            sent_errors.append({
                "wrong": str(wrong),
                "correct": str(correct),
                "type": str(row.get("type", "grammar")),
                "reason": str(row.get("reason", "")),
            })
        out.append(sent_errors)
    return out


def parse_language_detect_json(content: str) -> str | None:
    """Parse assistant message to extract the detected language string."""
    if not content or not content.strip():
        return None
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            data = repair_json_object(text)
        except Exception:
            return None
    if not isinstance(data, Mapping):
        return None
    root = cast("Mapping[str, Any]", data)
    lang = root.get("detected_language_bcp47")
    return str(lang) if isinstance(lang, str) else None


def parse_language_detect_batch_json(content: str) -> list[str | None]:
    """Parse assistant message into a list of detected languages."""
    if not content or not content.strip():
        return []
    text = content.strip()
    m = GRAMMAR_JSON_TAIL_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            data = repair_json_object(text)
        except Exception:
            return []
    if not isinstance(data, Mapping):
        return []
    root = cast("Mapping[str, Any]", data)
    results = root.get("results")
    if not isinstance(results, list):
        return []

    out: list[str | None] = []
    for res in results:
        if not isinstance(res, Mapping):
            out.append(None)
            continue
        lang = res.get("detected_language_bcp47")
        out.append(str(lang) if isinstance(lang, str) else None)
    return out


