# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core tool schemas for string eval.

Prefer a committed snapshot so string eval never needs soffice. When UNO
is importable, overlay live ``to_openai_schema`` from tool classes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_SNAPSHOT = Path(__file__).resolve().parent / "eval_core_schemas.json"

# (kind, module, class) — overlay only; snapshot is the no-UNO baseline.
_LIVE_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("writer", "plugin.writer.content", "GetDocumentContent"),
    ("writer", "plugin.writer.content", "ApplyDocumentContent"),
    ("writer", "plugin.writer.search", "SearchInDocument"),
    ("writer", "plugin.writer.specialized.comments", "AddComment"),
    ("draw", "plugin.draw.tree", "GetDrawTree"),
    ("draw", "plugin.draw.shapes", "UpsertShape"),
    ("draw", "plugin.draw.shapes", "ConnectShapes"),
    ("draw", "plugin.draw.shapes", "GetDrawSummary"),
    ("calc", "plugin.calc.cells", "ReadCellRange"),
    ("calc", "plugin.calc.cells", "WriteCellRange"),
    ("calc", "plugin.calc.cells", "SortRange"),
    ("calc", "plugin.calc.sheets", "GetSheetSummary"),
)


def load_schema_snapshot() -> dict[str, list[dict[str, Any]]]:
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, rows in data.items():
        if isinstance(rows, list):
            out[str(kind)] = [row for row in rows if isinstance(row, dict)]
    return out


def _try_live_schema(module_name: str, class_name: str) -> dict[str, Any] | None:
    try:
        import importlib

        from plugin.framework.tool import to_openai_schema

        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
        schema = to_openai_schema(cls())
    except Exception:
        return None
    return schema if isinstance(schema, dict) and schema.get("name") else None


def _merge_live(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    by_kind: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, rows in snapshot.items():
        by_kind[kind] = {str(row.get("name")): row for row in rows if row.get("name")}
    for kind, module_name, class_name in _LIVE_CLASSES:
        live = _try_live_schema(module_name, class_name)
        if not live:
            continue
        name = str(live.get("name"))
        bucket = by_kind.setdefault(kind, {})
        bucket[name] = live
    return {
        kind: list(names.values())
        for kind, names in by_kind.items()
    }


def schemas_for_kind(kind: str) -> list[dict[str, Any]]:
    """OpenAI function schemas for writer / draw / calc."""
    merged = _merge_live(load_schema_snapshot())
    key = kind if kind in merged else "writer"
    return list(merged.get(key) or [])


def build_eval_tool_schemas(*, kind: str) -> list[dict[str, Any]]:
    """Public catalog used by ``llm_chat_eval``."""
    return schemas_for_kind(kind)
