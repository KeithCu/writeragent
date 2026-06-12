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


class _HtmlTableParser(HTMLParser):
    """Minimal HTML table parser for PP-Structure table HTML output."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            if self._current_row is not None:
                self._current_row.append("".join(self._cell_parts).strip())
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _parse_html_table(html: str) -> tuple[list[str], list[list[str]]]:
    parser = _HtmlTableParser()
    try:
        parser.feed(html)
    except Exception:
        return [], []
    if not parser.rows:
        return [], []
    columns = [str(c) for c in parser.rows[0]]
    data_rows = [[str(c) for c in row] for row in parser.rows[1:]]
    if not columns and data_rows:
        width = max(len(r) for r in data_rows)
        columns = [f"col_{i + 1}" for i in range(width)]
    return columns, data_rows


def _text_from_structure_res(res: Any) -> str:
    if res is None:
        return ""
    if isinstance(res, str):
        return res.strip()
    if isinstance(res, dict):
        for key in ("text", "content", "html", "markdown"):
            val = res.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        html = res.get("html")
        if isinstance(html, str) and "<table" in html.lower():
            _cols, rows = _parse_html_table(html)
            if rows:
                return "\n".join("\t".join(row) for row in rows)
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
            columns, rows = _parse_html_table(html)
            if columns or rows:
                limited = rows[:MAX_TABLE_ROWS]
                return {
                    "name": name,
                    "columns": columns,
                    "rows": limited,
                    "truncated": len(rows) > MAX_TABLE_ROWS,
                    "total_rows": len(rows),
                }
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
