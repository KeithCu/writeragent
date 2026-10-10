# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PaddleOCR / PP-Structure backend for trusted vision helpers."""
from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

from plugin.vision.vision_common import (
    MAX_TABLE_ROWS,
    bbox_to_xywh,
    css_inline_unavailable_result,
    decode_image_bytes,
    error_result,
    is_css_inline_import_error,
    ok_result,
    table_from_span_cells,
    table_to_tsv_lines,
)

log = logging.getLogger(__name__)

_paddle_ocr_engine: Any = None
_paddle_ocr_lang: str | None = None
_pp_structure_engine: Any = None
_pp_structure_lang: str | None = None


def _get_paddle_ocr(lang: str) -> Any:
    """Lazy-init one PaddleOCR instance per worker process (module singleton)."""
    global _paddle_ocr_engine, _paddle_ocr_lang
    if _paddle_ocr_engine is not None and _paddle_ocr_lang == lang:
        return _paddle_ocr_engine
    # Guard (ImportError, AttributeError): paddleocr may not export PaddleOCR.
    # Callers catch constructor exceptions so they do not escape unhandled.
    try:
        paddleocr_mod = importlib.import_module("paddleocr")
        paddle_ocr_cls = paddleocr_mod.PaddleOCR
    except (ImportError, AttributeError) as exc:
        raise ImportError("paddleocr is not installed") from exc
    # show_log is a 2.x-only kwarg. 3.x does not list it in
    # _DEPRECATED_PARAM_NAME_MAPPING, so PaddleX raises
    # ValueError: Unknown argument: show_log (or TypeError on a strict
    # signature) and the engine never starts. 2.x defaults show_log off, so
    # omitting it still constructs. use_angle_cls is the 2.x flag and the 3.x
    # alias of use_textline_orientation.
    _paddle_ocr_engine = paddle_ocr_cls(use_angle_cls=True, lang=lang)
    _paddle_ocr_lang = lang
    return _paddle_ocr_engine


def _run_paddle_ocr(engine: Any, image_array: Any) -> Any:
    """Call 3.x ``predict`` or 2.x ``ocr(..., cls=True)``.

    3.x still has ``ocr``, but that method only forwards ``**kwargs`` to
    ``predict``. ``predict`` is keyword-only and has no ``cls``, so
    ``ocr(image, cls=True)`` raises TypeError and the Result parser never
    runs. 2.x defines ``ocr`` only; ``cls=True`` runs the angle classifier
    loaded with ``use_angle_cls``.
    """
    if hasattr(engine, "predict"):
        return engine.predict(image_array)
    if hasattr(engine, "ocr"):
        return engine.ocr(image_array, cls=True)
    raise RuntimeError("PaddleOCR engine has no ocr or predict method")


def _iter_pages(raw: Any) -> list[Any]:
    """List of pages from a list, tuple, or PaddleX generator. ``None`` pages dropped."""
    if isinstance(raw, Iterator):
        raw = list(raw)
    if raw is None:
        return []
    if isinstance(raw, tuple):
        raw = list(raw)
    if isinstance(raw, list):
        return [page for page in raw if page is not None]
    return [raw]


def _json_dict(obj: Any) -> dict[str, Any] | None:
    """``Result.json`` when it is a dict. Plain dicts and 2.x lists have no such view."""
    try:
        view = getattr(obj, "json", None)
    except Exception:
        # A broken Result.json must not hide the dict body (2.x regions, or the
        # live Result keys). Unexpected: the property is part of the PaddleX contract.
        log.exception("Paddle Result.json failed")
        return None
    return view if isinstance(view, dict) else None


def _looks_like_v3_payload(data: dict[str, Any]) -> bool:
    return "rec_texts" in data or "parsing_res_list" in data or "table_res_list" in data


def _paddle_payload(obj: Any) -> dict[str, Any] | None:
    """3.x OCR or PP-Structure page, or None for a 2.x line list / region.

    PaddleOCR 3.x ``ocr`` / ``predict`` returns a Result. ``Result.json`` and the
    printed form are ``{"res": payload}`` (``rec_texts`` / ``rec_scores`` /
    ``rec_polys``, or ``parsing_res_list`` + ``table_res_list``). The object is
    also a dict, but PP-Structure stores LayoutBlock instances there and the
    ``block_label`` schema only on ``.json``. 2.x regions use ``res`` for line
    text or ``{"html": ...}`` — those must not be unwrapped as a page.
    """
    candidates: list[dict[str, Any]] = []
    view = _json_dict(obj)
    if view is not None:
        candidates.append(view)
    if isinstance(obj, dict):
        candidates.append(obj)
    for data in candidates:
        inner = data.get("res")
        if isinstance(inner, dict) and _looks_like_v3_payload(inner):
            return inner
        if _looks_like_v3_payload(data):
            return data
    return None


def _seq(value: Any) -> Any:
    """None → empty. Leave numpy arrays alone; ``array or []`` raises on them."""
    return () if value is None else value


def _seq_item(seq: Any, index: int) -> Any:
    if seq is None:
        return None
    try:
        if index >= len(seq):
            return None
    except TypeError:
        return None
    return seq[index]


def _parse_ocr_v3(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """``rec_texts`` / ``rec_scores`` / ``rec_polys`` (``rec_boxes`` if no polys)."""
    regions: list[dict[str, Any]] = []
    texts: list[str] = []
    rec_texts = _seq(payload.get("rec_texts"))
    rec_scores = _seq(payload.get("rec_scores"))
    boxes = payload.get("rec_polys")
    if boxes is None:
        boxes = payload.get("rec_boxes")
    for index, text_raw in enumerate(rec_texts):
        text = str(text_raw or "").strip()
        if not text:
            continue
        score_raw = _seq_item(rec_scores, index)
        try:
            confidence = float(score_raw) if score_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        box_raw = _seq_item(boxes, index)
        regions.append(
            {
                "box": bbox_to_xywh(box_raw) if box_raw is not None else [0, 0, 0, 0],
                "text": text,
                "confidence": confidence,
            }
        )
        texts.append(text)
    return regions, texts


def _parse_ocr_v2_lines(raw_lines: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
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
                "box": bbox_to_xywh(box_raw),
                "text": text,
                "confidence": confidence,
            }
        )
        texts.append(text)
    return regions, texts


def _parse_ocr_lines(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """2.x ``[[box, (text, score)], ...]`` pages and 3.x Result pages.

    3.x stopped returning the line list. ``_run_paddle_ocr`` used to keep only a
    list page and otherwise return ``[]``, so ``extract_text`` reported
    "No text detected." with an empty ``full_text``.
    """
    regions: list[dict[str, Any]] = []
    texts: list[str] = []
    for page in _iter_pages(raw):
        payload = _paddle_payload(page)
        if isinstance(payload, dict) and "rec_texts" in payload:
            page_regions, page_texts = _parse_ocr_v3(payload)
        elif isinstance(page, list):
            page_regions, page_texts = _parse_ocr_v2_lines(page)
        else:
            continue
        regions.extend(page_regions)
        texts.extend(page_texts)
    return regions, texts


def extract_text(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_text"
    lang = str(params.get("lang") or "en").strip() or "en"
    # Constructor errors from _get_paddle_ocr become error_result. Leaving
    # them unhandled aborts the vision call.
    try:
        engine = _get_paddle_ocr(lang)
    except ImportError:
        return error_result(
            "PADDLEOCR_UNAVAILABLE",
            "Install paddleocr and paddlepaddle in your venv (Settings → Python): pip install paddleocr paddlepaddle numpy",
            helper=helper,
        )
    except Exception as exc:
        log.exception("PaddleOCR constructor failed")
        return error_result("VISION_ERROR", str(exc), helper=helper)

    try:
        image_array = decode_image_bytes(image)
        raw_lines = _run_paddle_ocr(engine, image_array)
        regions, texts = _parse_ocr_lines(raw_lines)
    except Exception as exc:
        log.exception("extract_text OCR failed")
        return error_result("VISION_ERROR", str(exc), helper=helper)

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
    line_count = len(texts)

    return ok_result(
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
    return table_from_span_cells(parser.cells, num_rows, num_cols, name=name)


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


def _append_table_plaintext(text_parts: list[str], table: dict[str, Any]) -> None:
    text_parts.extend(table_to_tsv_lines(table))


def _parsed_table(html: str, table_index: int) -> tuple[dict[str, Any], int] | None:
    if "<table" not in html.lower():
        return None
    table = _table_from_structure_res({"html": html}, name=f"table_{table_index + 1}")
    if not table:
        return None
    return table, table_index + 1


def _v3_block_fields(block: Any) -> tuple[str, str, Any]:
    """``block_label`` / ``block_content`` / ``block_bbox``, or a LayoutBlock."""
    if isinstance(block, dict):
        label = block.get("block_label")
        if label is None:
            label = block.get("label")
        content = block.get("block_content")
        if content is None:
            content = block.get("content")
        bbox = block.get("block_bbox")
        if bbox is None:
            bbox = block.get("bbox")
    else:
        label = getattr(block, "label", None)
        content = getattr(block, "content", None)
        bbox = getattr(block, "bbox", None)
    label_text = str(label).strip().lower() if label else "text"
    content_text = "" if content is None else str(content).strip()
    return label_text, content_text, bbox


def _v3_table_html(item: Any) -> str:
    sources: list[Any] = []
    view = _json_dict(item)
    if view is not None:
        sources.append(view)
    if isinstance(item, dict):
        sources.append(item)
    for source in sources:
        if not isinstance(source, dict):
            continue
        inner = source.get("res")
        if isinstance(inner, dict):
            source = inner
        html = source.get("pred_html")
        if not isinstance(html, str):
            html = source.get("html")
        if isinstance(html, str) and html.strip():
            return html
    return ""


def _is_v3_block(block: Any) -> bool:
    """True for ``block_label`` dicts and LayoutBlock, not a 2.x ``{type, bbox, res}``."""
    if isinstance(block, dict):
        return any(key in block for key in ("block_label", "block_content", "block_bbox"))
    if isinstance(block, (list, tuple, str, bytes)):
        return False
    return hasattr(block, "label") and hasattr(block, "content")


def _is_v3_structure_page(obj: Any) -> bool:
    payload = _paddle_payload(obj)
    if not isinstance(payload, dict):
        return False
    # ``table_res_list`` exists only on PPStructureV3 pages (``pred_html``).
    if "table_res_list" in payload:
        return True
    blocks = payload.get("parsing_res_list")
    if not isinstance(blocks, list):
        return False
    # A bare ``parsing_res_list`` of 2.x regions is unwrapped below. 3.x blocks
    # use block_label / block_content / block_bbox (or LayoutBlock attributes).
    if not blocks:
        return True
    return _is_v3_block(blocks[0])


def _parse_v3_structure_page(
    payload: dict[str, Any],
    *,
    table_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """One PPStructureV3 page: layout blocks plus ``table_res_list`` HTML."""
    htmls = [_v3_table_html(item) for item in _seq(payload.get("table_res_list"))]
    # Keep empty HTML slots. Dropping them with `if html` shifts indices, so
    # later tables attach to the wrong HTML or run out of slots. table_res_list
    # indices must line up 1:1 with table blocks.
    html_at = 0
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for block in _seq(payload.get("parsing_res_list")):
        label, content, bbox = _v3_block_fields(block)
        box = bbox_to_xywh(bbox) if bbox is not None else [0, 0, 0, 0]
        if label == "table":
            html = htmls[html_at] if html_at < len(htmls) else ""
            if html_at < len(htmls):
                html_at += 1
            if not html.strip():
                html = content
            parsed = _parsed_table(html, table_index)
            if parsed is not None:
                table, table_index = parsed
                tables.append(table)
                _append_table_plaintext(text_parts, table)
            # Table HTML is the grid (``tables[]``), not block prose. Same rule
            # as ``_text_from_structure_res`` for a 2.x ``res.html`` table.
            blocks.append({"type": "table", "text": "", "box": box})
            continue
        if not content:
            continue
        blocks.append({"type": label or "text", "text": content, "box": box})
        text_parts.append(content)

    for html in htmls[html_at:]:
        parsed = _parsed_table(html, table_index)
        if parsed is None:
            continue
        table, table_index = parsed
        tables.append(table)
        _append_table_plaintext(text_parts, table)
        blocks.append({"type": "table", "text": "", "box": [0, 0, 0, 0]})

    return blocks, tables, text_parts, table_index


def _normalize_structure_pages(raw: Any) -> list[Any]:
    """One entry per page. 2.x pages are region lists; 3.x pages stay Results.

    PPStructureV3.predict() yields per-page Results (often a generator). The old
    normalizer peeled ``parsing_res_list`` off that dict and dropped
    ``table_res_list``. LayoutBlock entries are not ``{type, bbox, res}``, so
    ``_parse_structure_output`` then returned no blocks, tables, or HTML.
    """
    if isinstance(raw, Iterator):
        raw = [page for page in raw if page is not None]
    if raw is None:
        return []
    if isinstance(raw, tuple):
        raw = list(raw)
    if _is_v3_structure_page(raw):
        return [raw]
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
    return [raw]


def _parse_structure_output(raw_pages: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    text_parts: list[str] = []
    table_index = 0

    for item in raw_pages:
        if _is_v3_structure_page(item):
            payload = _paddle_payload(item)
            if payload is None:
                continue
            page_blocks, page_tables, page_text, table_index = _parse_v3_structure_page(payload, table_index=table_index)
            blocks.extend(page_blocks)
            tables.extend(page_tables)
            text_parts.extend(page_text)
            continue
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or item.get("label") or "block").strip().lower()
        bbox = item.get("bbox") or item.get("box") or item.get("coordinate")
        res = item.get("res") if "res" in item else item.get("result")
        box = bbox_to_xywh(bbox) if bbox is not None else [0, 0, 0, 0]

        if block_type == "table" or (isinstance(res, dict) and "html" in res):
            table_index += 1
            table = _table_from_structure_res(res, name=f"table_{table_index}")
            if table:
                tables.append(table)
                _append_table_plaintext(text_parts, table)
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


def _get_pp_structure(lang: str = "en") -> Any:
    """Lazy-init one PPStructureV3 instance per worker process."""
    global _pp_structure_engine, _pp_structure_lang
    if _pp_structure_engine is not None and _pp_structure_lang == lang:
        return _pp_structure_engine
    try:
        paddleocr_mod = importlib.import_module("paddleocr")
        structure_cls = paddleocr_mod.PPStructureV3
    except (ImportError, AttributeError) as exc:
        raise ImportError("PPStructureV3 is not available") from exc
    # show_log is not a PPStructureV3 parameter. It lands in **kwargs, and
    # PaddleX raises ValueError: Unknown argument: show_log, so
    # extract_structure never starts on 3.x.
    # Pass lang and cache one engine per lang. Callers turn constructor
    # exceptions into error_result.
    _pp_structure_engine = structure_cls(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_table_recognition=True,
        lang=lang,
    )
    _pp_structure_lang = lang
    return _pp_structure_engine


def _run_pp_structure(engine: Any, image_array: Any) -> list[Any]:
    if hasattr(engine, "predict"):
        raw = engine.predict(image_array)
    else:
        raise RuntimeError("PPStructureV3 engine has no predict method")
    return _normalize_structure_pages(raw)


def extract_structure(image: Any, params: dict[str, Any]) -> dict[str, Any]:
    helper = "extract_structure"
    lang = str(params.get("lang") or "en").strip() or "en"
    # Constructor errors from _get_pp_structure become error_result with
    # VISION_ERROR. Leaving them unhandled aborts the vision call.
    try:
        engine = _get_pp_structure(lang)
    except ImportError:
        return error_result(
            "PADDLEOCR_UNAVAILABLE",
            "Install paddleocr and paddlepaddle in your venv (Settings → Python): pip install paddleocr paddlepaddle numpy",
            helper=helper,
        )
    except Exception as exc:
        log.exception("PPStructure constructor failed")
        return error_result("VISION_ERROR", str(exc), helper=helper)

    try:
        image_array = decode_image_bytes(image)
        raw_pages = _run_pp_structure(engine, image_array)
        blocks, tables, text_parts = _parse_structure_output(raw_pages)
    except Exception as exc:
        log.exception("extract_structure failed")
        return error_result("VISION_ERROR", str(exc), helper=helper)

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

    return ok_result(
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
