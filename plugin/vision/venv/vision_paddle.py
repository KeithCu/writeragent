# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PaddleOCR / PP-Structure backend for trusted vision helpers."""
from __future__ import annotations

import importlib
import logging
from html.parser import HTMLParser
from typing import Any

from plugin.vision.vision_common import (
    MAX_TABLE_ROWS,
    css_inline_unavailable_result,
    is_css_inline_import_error,
    _bbox_to_xywh,
    _box_to_xywh,
    _decode_image_bytes,
    _error_result,
    _ok_result,
)

log = logging.getLogger(__name__)

_paddle_ocr_engine: Any = None
_paddle_ocr_lang: str | None = None
_pp_structure_engine: Any = None


def _get_paddle_ocr(lang: str) -> Any:
    """Lazy-init one PaddleOCR instance per worker process (module singleton)."""
    global _paddle_ocr_engine, _paddle_ocr_lang
    if _paddle_ocr_engine is not None and _paddle_ocr_lang == lang:
        return _paddle_ocr_engine
    try:
        paddleocr_mod = importlib.import_module("paddleocr")
        paddle_ocr_cls = paddleocr_mod.PaddleOCR
    except ImportError as exc:
        raise ImportError("paddleocr is not installed") from exc
    _paddle_ocr_engine = paddle_ocr_cls(use_angle_cls=True, lang=lang, show_log=False)
    _paddle_ocr_lang = lang
    return _paddle_ocr_engine


def _run_paddle_ocr(engine: Any, image_array: Any) -> list[Any]:
    """Call PaddleOCR across 2.x/3.x API differences."""
    if hasattr(engine, "ocr"):
        result = engine.ocr(image_array, cls=True)
    elif hasattr(engine, "predict"):
        result = engine.predict(image_array)
    else:
        raise RuntimeError("PaddleOCR engine has no ocr or predict method")
    if not result:
        return []
    page = result[0] if isinstance(result, list) else result
    if not page:
        return []
    return list(page) if isinstance(page, list) else []


def _parse_ocr_lines(raw_lines: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    regions: list[dict[str, Any]] = []
    texts: list[str] = []
    for line in raw_lines:
        if not line or not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        box_raw, text_info = line[0], line[1]
        if isinstance(text_info, (list, tuple)) and text_info:
            text = str(text_info[0] or "").strip()
            confidence = float(text_info[1]) if len(text_info) > 1 else 0.0
        elif isinstance(text_info, str):
            text = text_info.strip()
            confidence = 0.0
        else:
            continue
        if not text:
            continue
        regions.append(
            {
                "box": _box_to_xywh(box_raw),
                "text": text,
                "confidence": confidence,
            }
        )
        texts.append(text)
    return regions, texts


def extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    lang = str(params.get("lang") or "en").strip() or "en"
    try:
        engine = _get_paddle_ocr(lang)
    except ImportError:
        return _error_result(
            "PADDLEOCR_UNAVAILABLE",
            "Install paddleocr and paddlepaddle in your venv (Settings → Python): pip install paddleocr paddlepaddle numpy",
            helper=helper,
        )

    try:
        image_array = _decode_image_bytes(image)
        raw_lines = _run_paddle_ocr(engine, image_array)
        regions, texts = _parse_ocr_lines(raw_lines)
    except Exception as exc:
        log.exception("extract_text OCR failed")
        return _error_result("VISION_ERROR", str(exc), helper=helper)

    from plugin.vision.venv.vision_html_export import html_from_paddle_regions

    try:
        html = html_from_paddle_regions(regions)
    except ImportError as exc:
        if is_css_inline_import_error(exc):
            return css_inline_unavailable_result(helper)
        raise
    full_text = "\n".join(texts)
    warnings: list[str] = []
    if not full_text:
        warnings.append("No text detected.")

    confidences = [float(r["confidence"]) for r in regions if r.get("confidence") is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    line_count = len(texts) if texts else (0 if not full_text else len(full_text.splitlines()))

    return _ok_result(
        helper,
        html=html,
        full_text=full_text,
        regions=regions,
        metrics={
            "line_count": line_count,
            "mean_confidence": mean_confidence,
            "engine": "paddle",
            "ocr_backend": "paddleocr",
        },
        warnings=warnings,
    )


def _positive_span(value: str | None) -> int:
    if value is None or not str(value).strip():
        return 1
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


class _HtmlTableParser(HTMLParser):
    """PP-Structure table HTML as origin cells with colspan/rowspan.

    The previous parser appended each cell's text in document order and
    ignored colspan/rowspan, so a merged header sat in column 0 and the
    next header landed under it. Slots covered by an earlier span are
    skipped, then ``_table_from_span_cells`` builds the same grid Docling uses.
    """

    _in_cell: bool
    _row: int
    _col: int
    _pending_rowspan: int
    _pending_colspan: int

    def __init__(self) -> None:
        super().__init__()
        self.cells: list[dict[str, Any]] = []
        self._row = -1
        self._col = 0
        self._cell_parts: list[str] = []
        self._in_cell = False
        self._pending_rowspan = 1
        self._pending_colspan = 1
        self._covered: set[tuple[int, int]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row += 1
            self._col = 0
            self._in_cell = False
        elif tag in ("td", "th"):
            if self._row < 0:
                self._row = 0
            while (self._row, self._col) in self._covered:
                self._col += 1
            attr_map = {name: value for name, value in attrs}
            self._pending_rowspan = _positive_span(attr_map.get("rowspan"))
            self._pending_colspan = _positive_span(attr_map.get("colspan"))
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            rowspan = self._pending_rowspan
            colspan = self._pending_colspan
            origin = (self._row, self._col)
            self.cells.append(
                {
                    "text": "".join(self._cell_parts).strip(),
                    "start_row_offset_idx": self._row,
                    "start_col_offset_idx": self._col,
                    "row_span": rowspan,
                    "col_span": colspan,
                }
            )
            for row_idx in range(self._row, self._row + rowspan):
                for col_idx in range(self._col, self._col + colspan):
                    if (row_idx, col_idx) != origin:
                        self._covered.add((row_idx, col_idx))
            self._col += colspan

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _table_from_html(html: str, *, name: str) -> dict[str, Any] | None:
    parser = _HtmlTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if not parser.cells:
        return None
    num_rows = 0
    num_cols = 0
    for cell in parser.cells:
        num_rows = max(num_rows, int(cell["start_row_offset_idx"]) + int(cell["row_span"]))
        num_cols = max(num_cols, int(cell["start_col_offset_idx"]) + int(cell["col_span"]))
    from plugin.vision.venv.vision_docling import _table_from_span_cells

    return _table_from_span_cells(parser.cells, num_rows, num_cols, name=name)


def _text_from_structure_res(res: Any) -> str:
    if res is None:
        return ""
    if isinstance(res, str):
        return res.strip()
    if isinstance(res, dict):
        for key in ("text", "content", "markdown"):
            val = res.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        html = res.get("html")
        if isinstance(html, str) and html.strip():
            # ``html`` used to be in the key loop above, so this branch never
            # ran and the raw ``<table>`` string became block text.
            # ``html_from_paddle_structure`` then escaped it into a ``<p>``
            # and also appended the parsed table. Table HTML is not prose;
            # the parsed grid is rendered separately.
            if "<table" in html.lower():
                return ""
            return html.strip()
        return ""
    if isinstance(res, list):
        parts: list[str] = []
        for item in res:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text).strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts)
    return str(res).strip()


def _table_from_structure_res(res: Any, *, name: str) -> dict[str, Any] | None:
    if res is None:
        return None
    if isinstance(res, dict):
        html = res.get("html")
        if isinstance(html, str) and html.strip():
            table = _table_from_html(html, name=name)
            if table:
                return table
        cell_block = res.get("cell_bbox") or res.get("cells")
        if isinstance(cell_block, list) and cell_block:
            rows = []
            for row in cell_block:
                if isinstance(row, list):
                    rows.append([str(c.get("text", c) if isinstance(c, dict) else c) for c in row])
            if rows:
                columns = rows[0]
                data = rows[1:] if len(rows) > 1 else []
                return {
                    "name": name,
                    "columns": columns,
                    "rows": data[:MAX_TABLE_ROWS],
                    "truncated": len(data) > MAX_TABLE_ROWS,
                    "total_rows": len(data),
                }
    return None


def _normalize_structure_pages(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        for key in ("layout_parsing_result", "parsing_res_list", "result", "res"):
            inner = raw.get(key)
            if isinstance(inner, list):
                return inner
        return [raw]
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            return list(raw[0])
        return raw
    if hasattr(raw, "__iter__"):
        return list(raw)
    return [raw]


def _parse_structure_output(raw_pages: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    text_parts: list[str] = []
    table_index = 0

    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or item.get("label") or "block").strip().lower()
        bbox = item.get("bbox") or item.get("box") or item.get("coordinate")
        res = item.get("res") if "res" in item else item.get("result")
        box = _bbox_to_xywh(bbox) if bbox is not None else [0, 0, 0, 0]

        if block_type == "table" or (isinstance(res, dict) and "html" in res):
            table_index += 1
            table = _table_from_structure_res(res, name=f"table_{table_index}")
            if table:
                tables.append(table)
                if table.get("columns"):
                    text_parts.append("\t".join(str(c) for c in table["columns"]))
                for row in table.get("rows") or []:
                    if isinstance(row, list):
                        text_parts.append("\t".join(str(c) for c in row))
            block_text = _text_from_structure_res(res)
            blocks.append({"type": "table", "text": block_text, "box": box})
            continue

        block_text = _text_from_structure_res(res)
        if not block_text and isinstance(item.get("text"), str):
            block_text = item["text"].strip()
        blocks.append({"type": block_type or "text", "text": block_text, "box": box})
        if block_text:
            text_parts.append(block_text)

    return blocks, tables, text_parts


def _get_pp_structure() -> Any:
    """Lazy-init one PPStructureV3 instance per worker process."""
    global _pp_structure_engine
    if _pp_structure_engine is not None:
        return _pp_structure_engine
    try:
        paddleocr_mod = importlib.import_module("paddleocr")
        structure_cls = paddleocr_mod.PPStructureV3
    except (ImportError, AttributeError) as exc:
        raise ImportError("PPStructureV3 is not available") from exc
    _pp_structure_engine = structure_cls(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_table_recognition=True,
        show_log=False,
    )
    return _pp_structure_engine


def _run_pp_structure(engine: Any, image_array: Any) -> list[Any]:
    if hasattr(engine, "predict"):
        raw = engine.predict(image_array)
    else:
        raise RuntimeError("PPStructureV3 engine has no predict method")
    return _normalize_structure_pages(raw)


def extract_structure(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_structure"
    del params  # lang reserved for future PP-Structure locale tuning
    try:
        engine = _get_pp_structure()
    except ImportError:
        return _error_result(
            "PADDLEOCR_UNAVAILABLE",
            "Install paddleocr and paddlepaddle in your venv (Settings → Python): pip install paddleocr paddlepaddle numpy",
            helper=helper,
        )

    try:
        image_array = _decode_image_bytes(image)
        raw_pages = _run_pp_structure(engine, image_array)
        blocks, tables, text_parts = _parse_structure_output(raw_pages)
    except Exception as exc:
        log.exception("extract_structure failed")
        return _error_result("VISION_ERROR", str(exc), helper=helper)

    from plugin.vision.venv.vision_html_export import html_from_paddle_structure

    full_text = "\n".join(text_parts)
    try:
        html = html_from_paddle_structure(blocks, tables)
    except ImportError as exc:
        if is_css_inline_import_error(exc):
            return css_inline_unavailable_result(helper)
        raise
    warnings: list[str] = []
    if not full_text and not tables and not blocks:
        warnings.append("No structure detected.")

    return _ok_result(
        helper,
        html=html,
        full_text=full_text,
        blocks=blocks,
        tables=tables,
        metrics={
            "block_count": len(blocks),
            "table_count": len(tables),
            "engine": "paddle",
            "ocr_backend": "ppstructure",
        },
        warnings=warnings,
    )
