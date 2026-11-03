# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nested-text oracle for WriterAgent container mapping.

Each container is one XText (body, table cell, footnote, frame). The cursor is
always (container, start, end). A range that would span two XText objects is a
model failure — the same class of bug as apply_document_content building a
cursor on model.getText() while the selection lived in a table cell.

This is not a LibreOffice harness: no keystrokes, layout, fonts, undo, or UNO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

class CrossContainerError(AssertionError):
    """A tool resolved a range against a different XText than the cursor."""


@dataclass
class NestedDocument:
    """Independent strings per container plus a single-container cursor."""

    body: str = ""
    tables: Dict[str, List[List[str]]] = field(default_factory=dict)
    footnotes: Dict[str, str] = field(default_factory=dict)
    frames: Dict[str, str] = field(default_factory=dict)
    cursor_kind: str = "body"
    cursor_id: str = ""
    cursor_start: int = 0
    cursor_end: int = 0
    # When True, apply/get/search on selection uses body even if the cursor is nested.
    use_body_for_nested: bool = False

    def container_key(self) -> Tuple[str, str]:
        return (self.cursor_kind, self.cursor_id)

    def get_text(self, kind: str | None = None, cid: str | None = None) -> str:
        kind = self.cursor_kind if kind is None else kind
        cid = self.cursor_id if cid is None else cid
        if kind == "body":
            return self.body
        if kind == "cell":
            table, cell = _split_cell_id(cid)
            row, col = _cell_rc(cell)
            return self.tables[table][row][col]
        if kind == "footnote":
            return self.footnotes[cid]
        if kind == "frame":
            return self.frames[cid]
        raise ValueError(f"unknown container kind {kind!r}")

    def set_text(self, value: str, kind: str | None = None, cid: str | None = None) -> None:
        kind = self.cursor_kind if kind is None else kind
        cid = self.cursor_id if cid is None else cid
        if kind == "body":
            self.body = value
            return
        if kind == "cell":
            table, cell = _split_cell_id(cid)
            row, col = _cell_rc(cell)
            self.tables[table][row][col] = value
            return
        if kind == "footnote":
            self.footnotes[cid] = value
            return
        if kind == "frame":
            self.frames[cid] = value
            return
        raise ValueError(f"unknown container kind {kind!r}")

    def check_cursor(self) -> None:
        text = self.get_text()
        if not (0 <= self.cursor_start <= self.cursor_end <= len(text)):
            raise CrossContainerError(
                f"cursor {(self.cursor_kind, self.cursor_id, self.cursor_start, self.cursor_end)} "
                f"out of range for container len={len(text)}"
            )

    def _resolve_for_cursor_op(self) -> Tuple[str, str]:
        """Which XText an apply/get/search should use given the cursor."""
        if self.use_body_for_nested and self.cursor_kind != "body":
            raise CrossContainerError(
                "tool used body XText while cursor is in "
                f"{self.cursor_kind}:{self.cursor_id}"
            )
        return self.cursor_kind, self.cursor_id

    def create_table(self, name: str, rows: int, cols: int) -> None:
        if rows < 1 or cols < 1:
            raise ValueError("table dimensions must be positive")
        self.tables[name] = [["" for _c in range(cols)] for _r in range(rows)]

    def set_cell(self, table: str, cell: str, text: str) -> None:
        row, col = _cell_rc(cell)
        self.tables[table][row][col] = text

    def insert_footnote(self, fn_id: str, text: str) -> None:
        self.footnotes[fn_id] = text

    def insert_frame(self, frame_id: str, text: str) -> None:
        self.frames[frame_id] = text

    def move_selection(self, kind: str, cid: str, start: int, end: int) -> None:
        self.cursor_kind = kind
        self.cursor_id = cid
        self.cursor_start = start
        self.cursor_end = end
        self.check_cursor()

    def delete_chars(self, count: int) -> None:
        kind, cid = self._resolve_for_cursor_op()
        text = self.get_text(kind, cid)
        start = self.cursor_start
        if self.cursor_start == self.cursor_end:
            end = min(start + max(count, 0), len(text))
        else:
            end = self.cursor_end
        new = text[:start] + text[end:]
        self.set_text(new, kind, cid)
        self.cursor_end = start
        self.check_cursor()

    def apply_content(self, content: str, target: str, old_content: str = "") -> None:
        if target == "full":
            self.body = content
            self.move_selection("body", "", 0, min(len(content), 0))
            return
        if target == "search":
            kind, cid, start = self._find(old_content)
            text = self.get_text(kind, cid)
            new = text[:start] + content + text[start + len(old_content) :]
            self.set_text(new, kind, cid)
            self.move_selection(kind, cid, start, start + len(content))
            return
        if target == "selection":
            kind, cid = self._resolve_for_cursor_op()
            text = self.get_text(kind, cid)
            start, end = self.cursor_start, self.cursor_end
            new = text[:start] + content + text[end:]
            self.set_text(new, kind, cid)
            self.cursor_end = start + len(content)
            self.check_cursor()
            return
        raise ValueError(f"unknown apply target {target!r}")

    def get_content(self, scope: str, start: int | None = None, end: int | None = None) -> str:
        if scope == "full":
            parts = [self.body]
            for _name, grid in self.tables.items():
                for row in grid:
                    parts.extend(row)
            parts.extend(self.footnotes.values())
            parts.extend(self.frames.values())
            return "\n".join(p for p in parts if p)
        if scope == "selection":
            kind, cid = self._resolve_for_cursor_op()
            text = self.get_text(kind, cid)
            return text[self.cursor_start : self.cursor_end]
        if scope == "range":
            if start is None or end is None:
                raise ValueError("range scope requires start and end")
            kind, cid = self._resolve_for_cursor_op()
            text = self.get_text(kind, cid)
            if not (0 <= start <= end <= len(text)):
                raise CrossContainerError("range offsets leave the cursor container")
            return text[start:end]
        raise ValueError(f"unknown get scope {scope!r}")

    def search_replace(self, old: str, new: str) -> int:
        kind, cid = self._resolve_for_cursor_op()
        text = self.get_text(kind, cid)
        count = text.count(old)
        self.set_text(text.replace(old, new), kind, cid)
        self.cursor_start = min(self.cursor_start, len(self.get_text(kind, cid)))
        self.cursor_end = min(self.cursor_end, len(self.get_text(kind, cid)))
        self.check_cursor()
        return count

    def _find(self, needle: str) -> Tuple[str, str, int]:
        if not needle:
            raise CrossContainerError("empty search needle")
        containers: List[Tuple[str, str, str]] = [("body", "", self.body)]
        for tname, grid in self.tables.items():
            for r, row in enumerate(grid):
                for c, cell in enumerate(row):
                    containers.append(("cell", f"{tname}:{_rc_cell(r, c)}", cell))
        for fn_id, text in self.footnotes.items():
            containers.append(("footnote", fn_id, text))
        for fr_id, text in self.frames.items():
            containers.append(("frame", fr_id, text))
        for kind, cid, text in containers:
            idx = text.find(needle)
            if idx >= 0:
                return kind, cid, idx
        raise CrossContainerError(f"needle {needle!r} not found")


def _split_cell_id(cid: str) -> Tuple[str, str]:
    table, cell = cid.split(":", 1)
    return table, cell


def _cell_rc(cell: str) -> Tuple[int, int]:
    col = 0
    i = 0
    while i < len(cell) and cell[i].isalpha():
        col = col * 26 + (ord(cell[i].upper()) - ord("A") + 1)
        i += 1
    row = int(cell[i:]) - 1
    return row, col - 1


def _rc_cell(row: int, col: int) -> str:
    n = col + 1
    letters = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters)) + str(row + 1)


def mineru_story() -> NestedDocument:
    """Canonical sequence: 3x2 table, MinerU in A2, select, delete 2, get, replace."""
    doc = NestedDocument()
    doc.create_table("Table1", 3, 2)
    doc.set_cell("Table1", "A2", "MinerU")
    # Collapsed cursor at the start of the cell, then delete two characters.
    doc.move_selection("cell", "Table1:A2", 0, 0)
    doc.delete_chars(2)
    doc.get_content("selection")
    doc.search_replace("ner", "nerX")
    return doc


def mineru_story_wrong_body() -> None:
    """Replay of the table-cell bug: apply uses body while cursor is in A2."""
    doc = NestedDocument(use_body_for_nested=True)
    doc.create_table("Table1", 3, 2)
    doc.set_cell("Table1", "A2", "MinerU")
    doc.move_selection("cell", "Table1:A2", 0, 6)
    doc.apply_content("MinerU-EDIT", target="selection")


Op = Tuple[str, Dict[str, Any]]

_STORY_OPS: Sequence[str] = (
    "create_table",
    "set_cell",
    "move_cell",
    "delete_chars",
    "get_selection",
    "apply_selection",
    "search_replace",
    "insert_footnote",
    "move_footnote",
)


def generate_story(rng: Any, n_ops: int = 12) -> List[Op]:
    """Weighted toward mutate-nested-then-read (same bias as the MCP fuzzer)."""
    ops: List[Op] = []
    has_table = False
    for i in range(n_ops):
        if not has_table or (i == 0):
            ops.append(("create_table", {"name": "Table1", "rows": 3, "cols": 2}))
            ops.append(("set_cell", {"table": "Table1", "cell": "A2", "text": "MinerU"}))
            has_table = True
            continue
        # Bias: after a mutate, follow with a read ~40% of the time.
        if ops and ops[-1][0] in {"set_cell", "delete_chars", "apply_selection", "search_replace"} and rng.random() < 0.4:
            ops.append(("get_selection", {}))
            continue
        choice = rng.choice(_STORY_OPS)
        if choice == "create_table":
            ops.append(("create_table", {"name": "Table1", "rows": 3, "cols": 2}))
        elif choice == "set_cell":
            ops.append(("set_cell", {"table": "Table1", "cell": rng.choice(["A1", "A2", "B1"]), "text": rng.choice(["MinerU", "ab", "x"])}))
        elif choice == "move_cell":
            cell = rng.choice(["A1", "A2", "B1"])
            ops.append(("move_cell", {"cid": f"Table1:{cell}", "start": 0, "end": 0}))
        elif choice == "delete_chars":
            ops.append(("delete_chars", {"count": rng.choice([1, 2])}))
        elif choice == "get_selection":
            ops.append(("get_selection", {}))
        elif choice == "apply_selection":
            ops.append(("apply_selection", {"content": rng.choice(["Z", "EDIT"])}))
        elif choice == "search_replace":
            ops.append(("search_replace", {"old": "e", "new": "E"}))
        elif choice == "insert_footnote":
            ops.append(("insert_footnote", {"fn_id": "fn_1", "text": "note"}))
        elif choice == "move_footnote":
            ops.append(("move_footnote", {"fn_id": "fn_1"}))
    return ops


def run_ops(doc: NestedDocument, ops: Sequence[Op]) -> NestedDocument:
    for name, kwargs in ops:
        _apply_op(doc, name, kwargs)
    return doc


def _apply_op(doc: NestedDocument, name: str, kwargs: Dict[str, Any]) -> None:
    if name == "create_table":
        if kwargs["name"] not in doc.tables:
            doc.create_table(kwargs["name"], kwargs["rows"], kwargs["cols"])
    elif name == "set_cell":
        if kwargs["table"] in doc.tables:
            doc.set_cell(kwargs["table"], kwargs["cell"], kwargs["text"])
    elif name == "move_cell":
        cid = kwargs["cid"]
        table, cell = _split_cell_id(cid)
        if table in doc.tables:
            row, col = _cell_rc(cell)
            text = doc.tables[table][row][col]
            start = min(kwargs["start"], len(text))
            end = min(max(kwargs["end"], start), len(text))
            doc.move_selection("cell", cid, start, end)
    elif name == "delete_chars":
        doc.delete_chars(kwargs["count"])
    elif name == "get_selection":
        doc.get_content("selection")
    elif name == "apply_selection":
        doc.apply_content(kwargs["content"], target="selection")
    elif name == "search_replace":
        doc.search_replace(kwargs["old"], kwargs["new"])
    elif name == "insert_footnote":
        doc.insert_footnote(kwargs["fn_id"], kwargs["text"])
    elif name == "move_footnote":
        if kwargs["fn_id"] in doc.footnotes:
            text = doc.footnotes[kwargs["fn_id"]]
            doc.move_selection("footnote", kwargs["fn_id"], 0, len(text))


def shrink_failing(ops: List[Op], predicate: Callable[[List[Op]], bool]) -> List[Op]:
    """Drop prefix/suffix steps while predicate (True = still fails) holds."""
    current = list(ops)
    changed = True
    while changed and len(current) > 1:
        changed = False
        for i in range(len(current)):
            candidate = current[:i] + current[i + 1 :]
            if not candidate:
                continue
            if predicate(candidate):
                current = candidate
                changed = True
                break
    return current
