# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
In-memory document for prompt_optimization benchmarks (no LibreOffice).

Implements a narrow subset of get_document_content / apply_document_content / find_text
JSON shapes so the LlmClient tool loop matches production tool names without UNO.
"""
from __future__ import annotations

import json
import re
from typing import Any

from plugin.framework.errors import safe_json_loads


def _normalize_apply_content(content: Any) -> str:
    """Mirror ApplyDocumentContent list/string normalization (content.py)."""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("[") and "<" in stripped:
            parsed = safe_json_loads(stripped)
            if isinstance(parsed, list):
                content = parsed
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    if isinstance(content, str):
        content = content.replace("\\n", "\n").replace("\\t", "\t")
    return content if isinstance(content, str) else ""


class StringDocState:
    """Mutable HTML (or plain) string standing in for a Writer document."""

    __slots__ = ("_html",)

    def __init__(self, initial: str) -> None:
        self._html = initial or ""

    def get_html(self) -> str:
        return self._html

    def set_html(self, html: str) -> None:
        self._html = html

    def get_document_content(self, **kwargs: Any) -> dict[str, Any]:
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        text = self._html
        if scope == "range":
            start = int(kwargs.get("start") or 0)
            end = int(kwargs.get("end") or len(text))
            start = max(0, min(start, len(text)))
            end = max(start, min(end, len(text)))
            text = text[start:end]
        elif scope == "selection":
            text = self._html
        if max_chars is not None and len(text) > int(max_chars):
            text = text[: int(max_chars)] + "\n\n[... truncated ...]"
        return {
            "status": "ok",
            "content": text,
            "length": len(text),
            "document_length": len(self._html),
        }

    def apply_document_content(self, **kwargs: Any) -> dict[str, Any]:
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        target = kwargs.get("target")
        if not target and old_content is not None:
            target = "search"
        if not target:
            return {
                "status": "error",
                "message": "Provide target or old_content for search.",
            }
        if target == "search" and old_content is None:
            return {"status": "error", "message": "target='search' requires old_content."}

        content = _normalize_apply_content(content)
        all_matches = bool(kwargs.get("all_matches", False))

        if target == "full_document":
            self._html = content
            return {"status": "ok", "message": "Replaced entire document."}
        if target == "end":
            self._html = self._html + content
            return {"status": "ok", "message": "Inserted content at end."}
        if target == "beginning":
            self._html = content + self._html
            return {"status": "ok", "message": "Inserted content at beginning."}
        if target == "selection":
            self._html = self._html + content
            return {"status": "ok", "message": "Inserted content (simulated selection)."}
        if target == "search":
            old = str(old_content)
            if old not in self._html:
                return {"status": "error", "message": "old_content not found in document."}
            if all_matches:
                self._html = self._html.replace(old, content)
                return {"status": "ok", "message": "Replaced all matches."}
            self._html = self._html.replace(old, content, 1)
            return {"status": "ok", "message": "Replaced first match."}
        return {"status": "error", "message": f"Unknown target: {target!r}"}

    def find_text(
        self,
        search: str,
        start: int = 0,
        limit: int | None = None,
        case_sensitive: bool = True,
    ) -> dict[str, Any]:
        if not search:
            return {"status": "error", "message": "search is required."}
        hay = self._html
        needle = search
        if not case_sensitive:
            hay_l = hay.lower()
            needle_l = needle.lower()
        else:
            hay_l = hay
            needle_l = needle
        ranges: list[dict[str, Any]] = []
        pos = max(0, start)
        while True:
            idx = hay_l.find(needle_l, pos)
            if idx == -1:
                break
            ranges.append(
                {
                    "start": idx,
                    "end": idx + len(search),
                    "text": hay[idx : idx + len(search)],
                }
            )
            pos = idx + 1
            if limit is not None and len(ranges) >= limit:
                break
        return {"status": "ok", "ranges": ranges}


def dispatch_string_tool(state: StringDocState, name: str, arguments_json: str) -> str:
    """Execute one tool by name; return JSON string for the assistant message."""
    try:
        args = safe_json_loads(arguments_json)
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        if name == "get_document_content":
            res = state.get_document_content(**args)
        elif name == "apply_document_content":
            res = state.apply_document_content(**args)
        elif name == "find_text":
            res = state.find_text(
                args.get("search", ""),
                start=int(args.get("start", 0)),
                limit=args.get("limit"),
                case_sensitive=bool(args.get("case_sensitive", True)),
            )
        else:
            res = {"status": "error", "message": f"Unknown tool: {name}"}
    except Exception as e:
        res = {"status": "error", "message": str(e)}
    return json.dumps(res, ensure_ascii=False)
