# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
In-memory document for prompt_optimization benchmarks (no LibreOffice).

Implements a narrow subset of get_document_content / apply_document_content / find_text
JSON shapes so the LlmClient tool loop matches production tool names without UNO.

Extended with basic DrawDocState for shapes/flowcharts (get_draw_tree, create_shape) and
CalcStringState for sorting and basic formula/column ops. This enables non-LO evaluation of
selected Calc tests (data sorting, tax column) from docs/archive/eval-ideas.md.
"""
from __future__ import annotations

import json
from typing import Any

from plugin.framework.errors import safe_json_loads


def _a1_to_col_row(addr: str) -> tuple[int, int]:
    """Parse A1 / C2 into 0-based (col, row)."""
    text = (addr or "").strip()
    if "." in text:
        text = text.split(".")[-1]
    letters = []
    digits = []
    for ch in text:
        if ch.isalpha():
            letters.append(ch.upper())
        elif ch.isdigit():
            digits.append(ch)
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    col = max(0, col - 1)
    row = max(0, int("".join(digits) or "1") - 1)
    return col, row


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

        # Mirror production's structured returns (content.py): on the search path use
        # replaced_count as the success signal (0 -> status "error", N>0 -> "ok").
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
            # Mirror production: old_content empty after stripping is a parameter error (no
            # replaced_count — the search never ran), not a match-everything no-op.
            old = str(old_content).strip()
            if not old:
                return {"status": "error", "message": "old_content is empty after normalization."}
            if old not in self._html:
                if all_matches:
                    return {"status": "error",
                            "message": "Replaced 0 occurrence(s). No matches found. Try a shorter substring.",
                            "replaced_count": 0}
                return {"status": "error",
                        "message": "old_content not found in document. Try a shorter, unique substring.",
                        "replaced_count": 0}
            if all_matches:
                count = self._html.count(old)
                if count == 0:
                    return {"status": "error",
                            "message": "Replaced 0 occurrence(s). No matches found. Try a shorter substring.",
                            "replaced_count": 0}
                self._html = self._html.replace(old, content)
                return {"status": "ok", "message": "Replaced %d occurrence(s)." % count,
                        "replaced_count": count}
            self._html = self._html.replace(old, content, 1)
            return {"status": "ok", "message": "Replaced 1 occurrence (by old_content).",
                    "replaced_count": 1}
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


class DrawDocState:
    """Simple in-memory state for Draw shapes and get_draw_tree (no LO).

    Supports flowchart tests from eval-ideas.md without screenshots. Maintains
    a list of shapes; builds semantic tree similar to plugin/draw/tree.py.
    """

    __slots__ = ("shapes", "_next_index")

    def __init__(self) -> None:
        self.shapes: list[dict[str, Any]] = []
        self._next_index = 0

    def shape_upsert(self, action: str = "create", index: int | None = None, shape_type: str = "rectangle", text: str = "", x: int = 1000, y: int = 1000, width: int = 2000, height: int = 1000, **kwargs: Any) -> dict[str, Any]:
        """Mock shape_upsert for flowchart and basic shapes."""
        if action == "create":
            idx = self._next_index
            self._next_index += 1

            entry = {
                "index": idx,
                "type": shape_type,
                "text": text,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "custom_shape_type": shape_type if "flowchart" in shape_type.lower() else None,
            }
            self.shapes.append(entry)
            return {
                "status": "ok",
                "message": f"Created {shape_type}",
                "index": idx,
                "page": 0,
                "shape_count_after": len(self.shapes),
            }
        elif action == "edit":
            if index is None:
                return {"status": "error", "message": "index is required for edit"}

            found = None
            for s in self.shapes:
                if s["index"] == index:
                    found = s
                    break

            if not found:
                return {"status": "error", "message": f"Shape index {index} not found"}

            if "x" in kwargs: found["x"] = kwargs["x"]
            if "y" in kwargs: found["y"] = kwargs["y"]
            if "width" in kwargs: found["width"] = kwargs["width"]
            if "height" in kwargs: found["height"] = kwargs["height"]
            if "text" in kwargs: found["text"] = kwargs["text"]

            return {
                "status": "ok",
                "message": "Shape updated",
                "page": 0,
            }
        return {"status": "error", "message": f"Unknown action {action}"}

    def get_draw_tree(self, **kwargs: Any) -> dict[str, Any]:
        """Returns semantic tree (DOM) matching production GetDrawTree."""
        tree = []
        for s in self.shapes:
            node = {
                "type": s["type"],
                "name": f"shape_{s['index']}",
                "text": s.get("text", ""),
                "geometry": {
                    "x": s["x"],
                    "y": s["y"],
                    "width": s["width"],
                    "height": s["height"],
                },
            }
            if s.get("custom_shape_type"):
                node["custom_shape_type"] = s["custom_shape_type"]
            tree.append(node)
        return {
            "status": "ok",
            "page_index": 0,
            "tree": tree,
        }

    def get_draw_summary(self, **kwargs: Any) -> dict[str, Any]:
        """Flat summary for compatibility."""
        return {
            "status": "ok",
            "page_index": 0,
            "shapes": [
                {
                    "index": s["index"],
                    "type": s["type"],
                    "x": s["x"],
                    "y": s["y"],
                    "width": s["width"],
                    "height": s["height"],
                    "text": s.get("text", ""),
                }
                for s in self.shapes
            ],
        }


class CalcStringState:
    """In-memory grid for non-LO Calc tests (data sorting, tax column from eval-ideas.md).

    Single active sheet as list-of-lists. Supports read/sort/write for range ops.
    Final snapshot returns JSON grid for judging (parallel to DrawDocState tree).
    """

    __slots__ = ("_grid", "_headers")

    def __init__(self, initial: str = "") -> None:
        self._grid: list[list[Any]] = []
        self._headers: list[str] = []
        if initial:
            self._parse_initial(initial)

    def _parse_initial(self, text: str) -> None:
        """Parse TSV/CSV-like initial document_content into grid."""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        for line in lines:
            if "\t" in line:
                row = [cell.strip() for cell in line.split("\t")]
            else:
                row = [cell.strip() for cell in line.split(",") if cell.strip()]
            if row:
                self._grid.append(row)
        if self._grid:
            self._headers = self._grid[0]

    def get_sheet_summary(self, **kwargs: Any) -> dict[str, Any]:
        """Simple summary matching get_calc_context_for_chat style."""
        rows = len(self._grid)
        cols = len(self._grid[0]) if self._grid else 0
        return {
            "status": "ok",
            "sheet_name": "Sheet1",
            "row_count": rows,
            "col_count": cols,
            "headers": self._headers,
            "grid": self._grid[:5],  # first few rows for judge
        }

    def sort_range(self, **kwargs: Any) -> dict[str, Any]:
        """Mock for sort_range (test 1). Sorts by column index or name."""
        if not self._grid or len(self._grid) < 2:
            return {"status": "ok", "message": "Nothing to sort"}
        col = kwargs.get("sort_column", "Revenue")
        ascending = kwargs.get("ascending", False)
        if isinstance(col, int):
            col_idx = col
            col_name = self._headers[col_idx] if 0 <= col_idx < len(self._headers) else str(col)
        else:
            col_name = str(col)
            try:
                col_idx = self._headers.index(col_name) if col_name in self._headers else 0
            except ValueError:
                col_idx = 0
        # Skip header, sort data rows by numeric or string value
        data_rows = self._grid[1:]
        data_rows.sort(key=lambda row: float(row[col_idx]) if row and len(row) > col_idx and str(row[col_idx]).replace(".", "").replace("-", "").isdigit() else row[col_idx], reverse=not ascending)
        self._grid = [self._grid[0]] + data_rows
        return {"status": "ok", "message": f"Sorted by column {col_idx} ({col_name})", "sorted_rows": len(data_rows)}

    def write_cell_range(self, **kwargs: Any) -> dict[str, Any]:
        """Write values to an A1 range (alias of production write_formula_range)."""
        return self.write_formula_range(**kwargs)

    def write_formula_range(self, **kwargs: Any) -> dict[str, Any]:
        """Write values to an A1 range so Tax header + rates land in the snapshot."""
        values = kwargs.get("values", [])
        if isinstance(values, str):
            parsed = safe_json_loads(values)
            if isinstance(parsed, list):
                values = parsed
            elif values:
                values = [values]
            else:
                values = []
        if not isinstance(values, list):
            values = [values]
        ranges = kwargs.get("range") or []
        if isinstance(ranges, str):
            ranges = [ranges]
        written = 0
        if ranges:
            for rng in ranges:
                written += self._write_a1_range(str(rng), values)
        elif self._grid and values:
            # Legacy: append/replace column C on data rows
            for i, row in enumerate(self._grid[1:]):
                if i < len(values):
                    if len(row) < 3:
                        row.extend([0] * (3 - len(row)))
                    row[2] = values[i]
                    written += 1
            if self._headers and len(self._headers) < 3:
                self._headers.extend([""] * (3 - len(self._headers)))
        return {"status": "ok", "message": "Wrote cell range (tax column applied)", "written": written}

    def _write_a1_range(self, rng: str, values: list[Any]) -> int:
        start, end = (rng.split(":", 1) + [rng])[:2]
        c0, r0 = _a1_to_col_row(start)
        c1, r1 = _a1_to_col_row(end)
        cells: list[tuple[int, int]] = []
        for row_i in range(r0, r1 + 1):
            for col_i in range(c0, c1 + 1):
                cells.append((row_i, col_i))
        if not cells:
            return 0
        if len(values) == 1 and len(cells) > 1:
            fill = values * len(cells)
        else:
            fill = values
        max_row = max(row_i for row_i, col_i in cells)
        max_col = max(col_i for row_i, col_i in cells)
        while len(self._grid) <= max_row:
            self._grid.append([])
        for row_i, col_i in cells:
            row = self._grid[row_i]
            while len(row) <= max_col:
                row.append("")
        for (row_i, col_i), val in zip(cells, fill):
            self._grid[row_i][col_i] = val
        if self._grid:
            self._headers = [str(c) if c is not None else "" for c in self._grid[0]]
        return min(len(fill), len(cells))

    def snapshot(self) -> dict[str, Any]:
        """JSON representation for final judging (like Draw tree)."""
        return {
            "status": "ok",
            "snapshot": True,
            "sheet": "Sheet1",
            "headers": self._headers,
            "rows": self._grid,
            "grid": self._grid,
            "row_count": len(self._grid),
        }


def dispatch_string_tool(state: StringDocState | DrawDocState | CalcStringState, name: str, arguments_json: str) -> str:
    """Execute one tool by name; return JSON string for the assistant message.

    Supports Writer (StringDocState), Draw (DrawDocState), and Calc (CalcStringState)
    for non-LO tests including data sorting and tax column.
    """
    try:
        args = safe_json_loads(arguments_json)
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        if isinstance(state, CalcStringState):
            if name == "sort_range":
                res = state.sort_range(**args)
            elif name in ("write_cell_range", "write_formula_range"):
                res = state.write_formula_range(**args)
            elif name in ("get_sheet_summary", "read_cell_range"):
                res = state.get_sheet_summary(**args)
            else:
                res = {"status": "error", "message": f"Unknown Calc tool: {name}"}
        elif isinstance(state, DrawDocState):
            if name == "shape_upsert":
                res = state.shape_upsert(**args)
            elif name in ("get_draw_tree", "shape_summary"):
                if name == "get_draw_tree":
                    res = state.get_draw_tree(**args)
                else:
                    res = state.get_draw_summary(**args)
            else:
                res = {"status": "error", "message": f"Unknown Draw tool: {name}"}
        elif isinstance(state, StringDocState):
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
                # Forward unknown to Draw or Calc if it looks like one (for mixed evals)
                if name in ("shape_upsert", "get_draw_tree", "shape_summary"):
                    draw_state = DrawDocState()
                    if name == "shape_upsert":
                        res = draw_state.shape_upsert(**args)
                    elif name == "get_draw_tree":
                        res = draw_state.get_draw_tree(**args)
                    else:
                        res = draw_state.get_draw_summary(**args)
                elif name in ("sort_range", "write_cell_range", "write_formula_range", "get_sheet_summary"):
                    # Fallback for mixed
                    calc_state = CalcStringState()
                    if name == "sort_range":
                        res = calc_state.sort_range(**args)
                    elif name in ("write_cell_range", "write_formula_range"):
                        res = calc_state.write_formula_range(**args)
                    else:
                        res = calc_state.get_sheet_summary(**args)
                else:
                    res = {"status": "error", "message": f"Unknown tool: {name}"}
        else:
            res = {"status": "error", "message": f"Unknown state type for tool {name}"}
    except Exception as e:
        res = {"status": "error", "message": str(e)}
    return json.dumps(res, ensure_ascii=False)
