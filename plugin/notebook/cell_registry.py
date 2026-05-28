# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Document-persisted notebook cell registry for interactive Writer notebooks."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from plugin.doc.document_helpers import get_document_property, set_document_property
from plugin.framework.json_utils import safe_json_loads

log = logging.getLogger("writeragent.notebook")

NOTEBOOK_REGISTRY_UDPROP = "WriterAgentNotebookJson"
NOTEBOOK_SOURCE_PATH_UDPROP = "WriterAgentNotebookSourcePath"
_REGISTRY_VERSION = 1

LastRunStatus = Literal["ok", "error"] | None


@dataclass
class NotebookCodeCell:
    """One imported code cell — stable ``cell_id`` survives renumbering in Phase 3."""

    cell_id: str
    index: int
    code_field_name: str
    execution_count: int | None = None
    output_start_bookmark: str = ""
    last_run_status: LastRunStatus = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotebookCodeCell:
        return cls(
            cell_id=str(data["cell_id"]),
            index=int(data["index"]),
            code_field_name=str(data["code_field_name"]),
            execution_count=data.get("execution_count"),
            output_start_bookmark=str(data.get("output_start_bookmark") or ""),
            last_run_status=data.get("last_run_status"),
        )


@dataclass
class NotebookDocState:
    version: int = _REGISTRY_VERSION
    source_path: str = ""
    code_cells: list[NotebookCodeCell] = field(default_factory=list)
    next_execution_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_path": self.source_path,
            "code_cells": [c.to_dict() for c in self.code_cells],
            "next_execution_count": self.next_execution_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotebookDocState:
        cells_raw = data.get("code_cells") or []
        cells = [NotebookCodeCell.from_dict(c) for c in cells_raw if isinstance(c, dict)]
        next_ec = data.get("next_execution_count")
        if next_ec is None:
            max_ec = 0
            for cell in cells:
                if cell.execution_count is not None:
                    max_ec = max(max_ec, int(cell.execution_count))
            next_ec = max_ec + 1 if max_ec else 1
        return cls(
            version=int(data.get("version") or _REGISTRY_VERSION),
            source_path=str(data.get("source_path") or ""),
            code_cells=cells,
            next_execution_count=int(next_ec),
        )


def cell_id_to_hex(cell_id: str) -> str:
    return cell_id.replace("-", "")


def cell_id_from_hex(hex_id: str) -> str | None:
    """Restore UUID from registry hex (32 chars) or return None if invalid."""
    h = (hex_id or "").strip().lower()
    if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
        return None
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def find_cell_by_hex(state: NotebookDocState, hex_id: str) -> NotebookCodeCell | None:
    uuid_form = cell_id_from_hex(hex_id)
    if uuid_form is None:
        return None
    for cell in state.code_cells:
        if cell.cell_id == uuid_form or cell_id_to_hex(cell.cell_id) == hex_id.strip().lower():
            return cell
    return None


def _bookmark_name_for_cell_id(cell_id: str) -> str:
    """LO bookmark name: ``nb_out_`` + hex id (no dashes)."""
    hex_id = cell_id.replace("-", "")
    return f"nb_out_{hex_id}"


def new_code_cell_entry(
    index: int,
    execution_count: Any | None,
    code_field_name: str,
) -> NotebookCodeCell:
    """Create a registry entry with a new stable ``cell_id`` and output bookmark name."""
    cell_id = str(uuid.uuid4())
    ec: int | None
    if execution_count is None:
        ec = None
    else:
        try:
            ec = int(execution_count)
        except (TypeError, ValueError):
            ec = None
    return NotebookCodeCell(
        cell_id=cell_id,
        index=index,
        code_field_name=code_field_name,
        execution_count=ec,
        output_start_bookmark=_bookmark_name_for_cell_id(cell_id),
    )


def state_to_json(state: NotebookDocState) -> str:
    return json.dumps(state.to_dict(), separators=(",", ":"))


def state_from_json(raw: str) -> NotebookDocState | None:
    """Parse registry JSON; return ``None`` on empty or corrupt payload."""
    if not (raw or "").strip():
        return None
    parsed = safe_json_loads(raw.strip())
    if not isinstance(parsed, dict):
        log.warning("notebook registry: expected object, got %s", type(parsed).__name__)
        return None
    version = parsed.get("version")
    if version != _REGISTRY_VERSION:
        log.warning("notebook registry: unsupported version %r", version)
        return None
    try:
        return NotebookDocState.from_dict(parsed)
    except (KeyError, TypeError, ValueError) as e:
        log.warning("notebook registry: invalid cell entry: %s", e)
        return None


def load_registry(doc: Any) -> NotebookDocState | None:
    raw = get_document_property(doc, NOTEBOOK_REGISTRY_UDPROP, default=None)
    if raw is None:
        return None
    return state_from_json(str(raw))


def save_registry(doc: Any, state: NotebookDocState) -> None:
    """Persist registry; replaces any previous notebook metadata on the document."""
    state.version = _REGISTRY_VERSION
    set_document_property(doc, NOTEBOOK_REGISTRY_UDPROP, state_to_json(state))


def has_notebook_registry(doc: Any) -> bool:
    state = load_registry(doc)
    return state is not None and len(state.code_cells) > 0


def save_notebook_source_path(doc: Any, path: str) -> None:
    if path:
        set_document_property(doc, NOTEBOOK_SOURCE_PATH_UDPROP, path)


def insert_output_start_bookmark(doc: Any, bookmark_name: str) -> bool:
    """Insert a point bookmark at the document end (after the Output heading was appended).

    Must run on the LO main thread. Returns False when bookmarks are unsupported or insert fails.
    """
    if not bookmark_name:
        return False
    try:
        if not hasattr(doc, "getBookmarks"):
            return False
        bookmarks = doc.getBookmarks()
        if bookmarks.hasByName(bookmark_name):
            log.debug("notebook registry: bookmark %r already exists", bookmark_name)
            return True
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        bookmark = doc.createInstance("com.sun.star.text.Bookmark")
        bookmark.Name = bookmark_name
        text.insertTextContent(cursor, bookmark, False)
        return True
    except Exception:
        log.exception("notebook registry: failed to insert bookmark %r", bookmark_name)
        return False
