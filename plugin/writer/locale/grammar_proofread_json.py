# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse grammar and language-detect JSON replies from the grammar LLM."""

from __future__ import annotations

from typing import Any, Mapping, cast

from plugin.framework.json_utils import safe_json_loads


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return None


def _coerce_json_object(content: str) -> Mapping[str, Any] | None:
    """Extract and parse a JSON object from assistant response text."""
    if not content or not content.strip():
        return None
    text = content.strip()
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        text = text[start : end + 1]
    return _as_mapping(safe_json_loads(text))


def _parse_error_item(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map one LLM error object to ``wrong``/``correct``/``type``/``reason``, or None if unusable."""
    wrong = row.get("wrong")
    correct = row.get("correct")
    # ``str(None)`` used to become reason "None" (rule id ``wa_g_None``) and a
    # ``(grammar) None`` tooltip; a non-string ``wrong`` could even match text.
    if not isinstance(wrong, str) or not isinstance(correct, str):
        return None
    typ = row.get("type")
    typ = typ.strip().lower() if isinstance(typ, str) else ""
    if typ not in ("grammar", "style", "spelling"):
        typ = "grammar"
    reason = row.get("reason")
    return {
        "wrong": wrong,
        "correct": correct,
        "type": typ,
        "reason": reason.strip() if isinstance(reason, str) else "",
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
    if not isinstance(lang, str):
        return None
    return lang.strip() or None


def _root_or_none(content: str, key: str) -> Mapping[str, Any] | None:
    """Grammar reply root, or None when the reply is empty or ``{}`` (clean-sentence policy)."""
    root = _coerce_json_object(content)
    if root is None:
        if not content.strip():
            return None
        # A non-empty unusable reply must not become a clean cache row. The
        # worker's handler turns this into a failed status; the next
        # proofreading pass retries because no row was written.
        raise ValueError("grammar JSON reply did not parse")
    if not root:
        # Some models answer "no issues" with a bare {}; that is clean, not a failure.
        return None
    if not isinstance(root.get(key), list):
        raise ValueError(f"grammar JSON reply has no {key} list")
    return root


def parse_grammar_json(content: str) -> list[dict[str, Any]]:
    """Parse a grammar reply. Empty body -> []; unusable body raises ValueError."""
    root = _root_or_none(content, "errors")
    return [] if root is None else _errors_of(root)


def parse_grammar_batch_json(content: str) -> list[list[dict[str, Any]]]:
    """Parse a batch grammar reply. Empty body -> []; unusable body raises ValueError."""
    root = _root_or_none(content, "results")
    if root is None:
        return []
    results = root.get("results")
    assert isinstance(results, list)  # _root_or_none checked the same key
    out: list[list[dict[str, Any]]] = []
    for res in results:
        mapped = _as_mapping(res)
        out.append([] if mapped is None else _errors_of(mapped))
    return out


def parse_language_detect_json(content: str) -> str | None:
    """Parse a language-detect reply; None when absent or unusable (caller falls back)."""
    root = _coerce_json_object(content)
    return None if root is None else _detected_language(root)


def parse_language_detect_batch_json(content: str) -> list[str | None]:
    """Parse a batch language-detect reply; empty when absent or unusable."""
    root = _coerce_json_object(content)
    results = None if root is None else root.get("results")
    if not isinstance(results, list):
        return []
    out: list[str | None] = []
    for res in results:
        mapped = _as_mapping(res)
        out.append(None if mapped is None else _detected_language(mapped))
    return out
