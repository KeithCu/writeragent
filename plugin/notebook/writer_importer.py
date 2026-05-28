# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Import a Jupyter .ipynb into Writer: body text for display, form fields for editable code."""

from __future__ import annotations

import base64
import logging
import os
import re
import struct
import tempfile
import time
from typing import Any

from com.sun.star.awt import Point, Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.contrib.nbformat import read_ipynb
from plugin.framework.i18n import _
from plugin.notebook.cell_registry import (
    NotebookDocState,
    cell_id_to_hex,
    insert_output_start_bookmark,
    new_code_cell_entry,
    save_notebook_source_path,
    save_registry,
)
from plugin.writer.images.image_tools import (
    _apply_graphic_properties,
    _create_embedded_graphic,
    _file_url_for_path,
    _mm_to_units,
    insert_image_at_locator,
)

log = logging.getLogger("writeragent.notebook")

# 1/100 mm — draw-page code field width
_DEFAULT_WIDTH = 14000
_MIN_FIELD_HEIGHT = 600
_LINE_HEIGHT = 380
_MAX_FIELD_HEIGHT = 20000
_STACK_MARGIN_X = 5000
_STACK_GAP = 400
_STACK_INITIAL_BOTTOM = 800
_RUN_BUTTON_SIZE = 600
# com.sun.star.form.FormButtonType.URL — numeric avoids import when UNO stubs are absent (pytest).
_FORM_BUTTON_URL = 1
_PROGRESS_EVERY_N_CELLS = 10
_SLOW_ADD_MS = 2000
_MAX_IMPORT_TEXT_CHARS = 50_000
_TRUNCATION_SUFFIX = "\n\n[… truncated for import …]"
_MAX_OUTPUTS_PER_CELL = 200
_MAX_IMAGE_DECODE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_DISPLAY_WIDTH_MM = 140
_DEFAULT_IMAGE_HEIGHT_MM = 80
_IMAGE_MIME_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg"}

# Writer paragraph styles (document locale usually provides these English names).
_STYLE_CELL_HEADING = "Heading 3"
_STYLE_SECTION_HEADING = "Heading 4"
_STYLE_OUTPUT = "Preformatted Text"
_STYLE_BODY = "Text Body"

# Auto-created on import for Jupyter-like [In [n]] gutter (1/100 mm margins).
_STYLE_NOTEBOOK_IN = "WriterAgent Notebook In"
_NOTEBOOK_IN_CHAR_HEIGHT = 9
_NOTEBOOK_IN_MARGIN_TOP = 0
_NOTEBOOK_IN_MARGIN_BOTTOM = 40

_PARAGRAPH_BREAK = 0  # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_HTML_TAG_RE = re.compile(r"<\s*[a-zA-Z]", re.DOTALL)


def _mono_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class _ImportStackCursor:
    """O(1) vertical stacking for code-cell form controls on the draw page."""

    __slots__ = ("_margin_x", "_gap", "_max_bottom", "shape_count")

    def __init__(self, dp: Any) -> None:
        self._margin_x = _STACK_MARGIN_X
        self._gap = _STACK_GAP
        self._max_bottom = _STACK_INITIAL_BOTTOM
        self.shape_count = 0
        self._seed_from_draw_page(dp)

    def _seed_from_draw_page(self, dp: Any) -> None:
        try:
            count = dp.getCount()
        except Exception:
            log.debug("draw page getCount failed during stack seed", exc_info=True)
            return
        for i in range(count):
            try:
                s = dp.getByIndex(i)
                pos = s.getPosition()
                sz = s.getSize()
                self._max_bottom = max(self._max_bottom, pos.Y + sz.Height)
                self.shape_count += 1
            except Exception:
                continue

    def place(self, height: int) -> Point:
        y = self._max_bottom + self._gap
        self._max_bottom = y + height
        self.shape_count += 1
        return Point(self._margin_x, y)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _coerce_notebook_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(line) for line in value)
    return str(value)


def _height_for_text(text: str) -> int:
    lines = max(1, (text or "").count("\n") + 1)
    return min(_MAX_FIELD_HEIGHT, max(_MIN_FIELD_HEIGHT, lines * _LINE_HEIGHT))


def _prepare_display_text(text: str) -> tuple[str, bool]:
    display = text or ""
    if len(display) <= _MAX_IMPORT_TEXT_CHARS:
        return display, False
    keep = max(0, _MAX_IMPORT_TEXT_CHARS - len(_TRUNCATION_SUFFIX))
    return display[:keep] + _TRUNCATION_SUFFIX, True


def _mime_plain(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data) if data is not None else ""
    if "text/plain" in data:
        plain = data["text/plain"]
        return plain if isinstance(plain, str) else "".join(plain)
    for key in sorted(data.keys()):
        if key.startswith("text/"):
            val = data[key]
            return val if isinstance(val, str) else "".join(val)
    return ""


def format_output_text(output: Any) -> str:
    """Turn one nbformat output object into plain text for the document body."""
    output_type = getattr(output, "output_type", None) or output.get("output_type", "")
    if output_type == "stream":
        name = getattr(output, "name", None) or output.get("name", "stdout")
        text = _coerce_notebook_text(getattr(output, "text", None) or output.get("text", ""))
        return f"[{name}]\n{text}"
    if output_type == "error":
        tb = getattr(output, "traceback", None) or output.get("traceback", "")
        if isinstance(tb, list):
            tb = "\n".join(tb)
        return _strip_ansi(str(tb))
    if output_type in ("execute_result", "display_data"):
        data = getattr(output, "data", None) or output.get("data", {})
        if isinstance(data, dict):
            if _notebook_image_payload(data) is not None:
                return ""
            plain = _mime_plain(data)
            if plain:
                return plain
            mime_types = ", ".join(sorted(data.keys()))
            return f"[non-text output: {mime_types}]"
    return str(output)


def format_all_outputs(outputs: list[Any]) -> str:
    parts = [format_output_text(o) for o in (outputs or [])]
    return "\n\n".join(p for p in parts if p.strip())


def _format_outputs_for_body(outputs: list[Any], cell_index: int) -> str:
    out_list = outputs or []
    if len(out_list) > _MAX_OUTPUTS_PER_CELL:
        log.warning(
            "notebook import cell=%d truncating outputs %d -> %d",
            cell_index,
            len(out_list),
            _MAX_OUTPUTS_PER_CELL,
        )
        out_list = out_list[:_MAX_OUTPUTS_PER_CELL]
    parts: list[str] = []
    for output in out_list:
        text = format_output_text(output)
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def _notebook_image_payload(data: dict[str, Any]) -> tuple[str, str] | None:
    """Return (mime, base64) for the first supported image bundle in a notebook output."""
    for mime in ("image/png", "image/jpeg", "image/jpg"):
        if mime in data:
            b64 = _coerce_notebook_text(data[mime])
            if b64.strip():
                return mime, b64
    return None


def _png_pixel_size(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", raw[16:24])
    if w < 1 or h < 1:
        return None
    return w, h


def _display_size_units(raw: bytes, mime: str) -> tuple[int, int]:
    """Map decoded image bytes to Writer size in 1/100 mm (capped width)."""
    px_size = _png_pixel_size(raw) if mime == "image/png" else None
    if px_size is not None:
        px_w, px_h = px_size
    else:
        px_w, px_h = None, None
    if px_w and px_h:
        w_mm = px_w * 25.4 / 96
        h_mm = px_h * 25.4 / 96
        if w_mm > _MAX_IMAGE_DISPLAY_WIDTH_MM:
            scale = _MAX_IMAGE_DISPLAY_WIDTH_MM / w_mm
            w_mm = _MAX_IMAGE_DISPLAY_WIDTH_MM
            h_mm = h_mm * scale
        return _mm_to_units(w_mm, h_mm)
    return _mm_to_units(_MAX_IMAGE_DISPLAY_WIDTH_MM, _DEFAULT_IMAGE_HEIGHT_MM)


def _decode_notebook_image(b64_data: str) -> bytes | None:
    b64_data = _coerce_notebook_text(b64_data)
    if len(b64_data) > _MAX_IMAGE_DECODE_BYTES:
        log.warning(
            "notebook import skip image decode size=%d max=%d",
            len(b64_data),
            _MAX_IMAGE_DECODE_BYTES,
        )
        return None
    try:
        return base64.b64decode(b64_data, validate=False)
    except Exception:
        log.debug("notebook image base64 decode failed", exc_info=True)
        return None


def _insert_image_in_flow(
    doc: Any,
    *,
    raw: bytes,
    mime: str,
    images_before: int,
    ctx: Any | None = None,
) -> bool:
    """Embed notebook image output in document flow at body end (TextGraphicObject)."""
    suffix = _IMAGE_MIME_SUFFIX.get(mime, ".png")
    tmp_path = None
    t0 = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        w_units, h_units = _display_size_units(raw, mime)
        w_mm = w_units / 100.0
        h_mm = h_units / 100.0
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        t_add = time.monotonic()
        if ctx is not None:
            graphic = insert_image_at_locator(
                ctx,
                doc,
                tmp_path,
                width_mm=w_mm,
                height_mm=h_mm,
                title="Notebook output",
                description=mime,
                text_cursor=cursor,
            )
            if graphic is None:
                raise RuntimeError("insert_image_at_locator returned None")
        else:
            image = _create_embedded_graphic(doc, "writer", _file_url_for_path(tmp_path), ctx=ctx)
            _apply_graphic_properties(
                image,
                width=w_units,
                height=h_units,
                title="Notebook output",
                description=mime,
                anchor_type=AS_CHARACTER,
                inside="writer",
            )
            text.insertTextContent(cursor, image, False)
        add_ms = _mono_ms(t_add)
        _log_shape_add(
            step="image",
            text_chars=len(raw),
            shape_h=h_units,
            shapes_before=images_before,
            create_ms=_mono_ms(t0),
            add_ms=add_ms,
        )
        return True
    except Exception:
        log.exception("Failed to insert notebook image in document flow")
        _log_shape_add(step="image", shapes_before=images_before, create_ms=_mono_ms(t0), add_ms=0, ok=False)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _outputs_contain_image(outputs: list[Any]) -> bool:
    for output in outputs or []:
        output_type = getattr(output, "output_type", None) or output.get("output_type", "")
        if output_type not in ("display_data", "execute_result"):
            continue
        data = getattr(output, "data", None) or output.get("data", {})
        if isinstance(data, dict) and _notebook_image_payload(data) is not None:
            return True
    return False


def _import_image_outputs_in_flow(
    doc: Any,
    outputs: list[Any],
    cell_index: int,
    *,
    images_before: int,
    ctx: Any | None = None,
) -> int:
    """Insert image/png/jpeg outputs in the document body. Returns number of images added."""
    added = 0
    out_list = outputs or []
    if len(out_list) > _MAX_OUTPUTS_PER_CELL:
        out_list = out_list[:_MAX_OUTPUTS_PER_CELL]
    for output in out_list:
        output_type = getattr(output, "output_type", None) or output.get("output_type", "")
        if output_type not in ("display_data", "execute_result"):
            continue
        data = getattr(output, "data", None) or output.get("data", {})
        if not isinstance(data, dict):
            continue
        payload = _notebook_image_payload(data)
        if payload is None:
            continue
        mime, b64 = payload
        raw = _decode_notebook_image(b64)
        if raw and _insert_image_in_flow(doc, raw=raw, mime=mime, images_before=images_before + added, ctx=ctx):
            added += 1
        else:
            log.debug("notebook import cell=%d skip image mime=%s", cell_index, mime)
    return added


def _log_shape_add(
    *,
    step: str,
    name: str = "",
    text_chars: int = 0,
    truncated: bool = False,
    shape_h: int = 0,
    shapes_before: int,
    create_ms: int = 0,
    text_ms: int = 0,
    add_ms: int = 0,
    ok: bool = True,
) -> None:
    total_ms = create_ms + text_ms + add_ms
    log.debug(
        "notebook import add step=%s name=%s text_chars=%d truncated=%s shape_h=%d shapes_before=%d "
        "create_ms=%d text_ms=%d add_ms=%d ok=%s",
        step,
        name,
        text_chars,
        truncated,
        shape_h,
        shapes_before,
        create_ms,
        text_ms,
        add_ms,
        ok,
    )
    if total_ms >= _SLOW_ADD_MS:
        log.warning(
            "notebook import slow UNO add step=%s total_ms=%d shapes_before=%d",
            step,
            total_ms,
            shapes_before,
        )


def flush_ui_idle(ctx: Any | None) -> None:
    if ctx is None:
        return
    try:
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(ctx)
        if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()
    except Exception:
        log.debug("processEventsToIdle failed", exc_info=True)


def _resolve_para_style(doc: Any, style_name: str | None) -> str | None:
    """Map English style label to a name that exists in this document (locale-safe)."""
    if not style_name:
        return None
    try:
        para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        log.debug("notebook import could not enumerate ParagraphStyles for %r", style_name)
    return None


def _get_para_styles(doc: Any) -> Any | None:
    try:
        return doc.getStyleFamilies().getByName("ParagraphStyles")
    except Exception:
        log.debug("notebook import could not get ParagraphStyles", exc_info=True)
        return None


def _create_import_para_style(
    doc: Any,
    para_styles: Any,
    style_name: str,
    *,
    parent_style: str,
    property_updates: dict[str, Any],
) -> bool:
    """Register a paragraph style if missing. Returns True when the style exists afterward."""
    if para_styles.hasByName(style_name):
        return True
    try:
        new_style = doc.createInstance("com.sun.star.style.ParagraphStyle")
        if new_style is None:
            return False
        resolved_parent = _resolve_para_style(doc, parent_style) or parent_style
        try:
            new_style.setParentStyle(resolved_parent)
        except Exception:
            log.debug("notebook import parent %r for %r failed", resolved_parent, style_name, exc_info=True)
        for prop_name, prop_val in property_updates.items():
            try:
                new_style.setPropertyValue(prop_name, prop_val)
            except Exception:
                log.debug("notebook import could not set %s on %r", prop_name, style_name, exc_info=True)
        para_styles.insertByName(style_name, new_style)
        return True
    except Exception:
        log.debug("notebook import failed to create style %r", style_name, exc_info=True)
        return False


def _ensure_notebook_import_styles(doc: Any) -> str | None:
    """Create notebook [In [n]] gutter style once per document; return resolved name."""
    para_styles = _get_para_styles(doc)
    if para_styles is None:
        return None
    parent_heading = _resolve_para_style(doc, _STYLE_CELL_HEADING) or "Heading 3"
    
    property_updates: dict[str, Any] = {
        "ParaAdjust": 0,
        "ParaLeftMargin": -1270,  # Out-dented by 1/2 inch (12.7 mm)
        "ParaTopMargin": _NOTEBOOK_IN_MARGIN_TOP,
        "ParaBottomMargin": _NOTEBOOK_IN_MARGIN_BOTTOM,
    }
    
    try:
        import uno
        from typing import cast
        ts = cast("Any", uno.createUnoStruct("com.sun.star.style.TabStop"))
        ts.Position = 1270  # Shift text after tab to start exactly at the normal page margin (0 cm relative)
        ts.Alignment = uno.getConstantByName("com.sun.star.style.TabAlign.LEFT")
        ts.DecimalChar = 46
        ts.FillChar = 32
        property_updates["ParaTabStops"] = (ts,)
    except Exception as e:
        log.debug("notebook import could not create TabStop struct: %s", e)

    _create_import_para_style(
        doc,
        para_styles,
        _STYLE_NOTEBOOK_IN,
        parent_style=parent_heading,
        property_updates=property_updates,
    )
    return _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)


def _format_in_prompt(execution_count: Any | None) -> str:
    if execution_count is None:
        return "[In [ ]]"
    return f"[In [{execution_count}]]"


def _append_in_prompt(
    doc: Any,
    execution_count: Any | None,
    *,
    in_style: str | None,
    lead_break: bool,
) -> None:
    _append_body_paragraph(doc, _format_in_prompt(execution_count), in_style, lead_break=lead_break)


def _looks_like_html(text: str) -> bool:
    return bool(_HTML_TAG_RE.search((text or "").strip()))


def _wrap_html_fragment(html: str) -> str:
    body = (html or "").strip()
    if re.search(r"(?is)<\s*html\b", body):
        return body
    return f"<html><body>{body}</body></html>"


def _doc_body_nonempty(doc: Any) -> bool:
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        return bool((cursor.getString() or "").strip())
    except Exception:
        return True


def _append_body_paragraph(doc: Any, content: str, para_style: str | None, *, lead_break: bool) -> None:
    """Append one paragraph to the Writer body (end of document)."""
    if not content and not para_style:
        return
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    if lead_break and _doc_body_nonempty(doc):
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        cursor.gotoEnd(False)
    resolved = _resolve_para_style(doc, para_style)
    if resolved:
        try:
            cursor.setPropertyValue("ParaStyleName", resolved)
        except Exception:
            log.debug("notebook import ParaStyleName %r not applied", resolved)
    text.insertString(cursor, content, False)


def _append_body_text_block(
    doc: Any,
    block: str,
    para_style: str | None,
    *,
    lead_break: bool = True,
) -> None:
    """Append one paragraph; internal newlines stay in the same block."""
    display, _ = _prepare_display_text(block)
    if not display:
        return
    _append_body_paragraph(doc, display, para_style, lead_break=lead_break)


def _append_markdown_cell(doc: Any, source: str, *, lead_break: bool) -> None:
    """Markdown cell: HTML fragments via StarWriter filter; else plain text body."""
    display, _ = _prepare_display_text(source)
    if not display:
        return
    if _looks_like_html(display):
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        if lead_break and _doc_body_nonempty(doc):
            text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
            cursor.gotoEnd(False)
        from plugin.writer.ops import insert_html_at_cursor

        try:
            insert_html_at_cursor(cursor, _wrap_html_fragment(display))
        except Exception:
            log.exception("notebook import HTML insert failed; falling back to plain text")
            _append_body_paragraph(doc, display, _STYLE_BODY, lead_break=False)
    else:
        _append_body_text_block(doc, display, _STYLE_BODY, lead_break=lead_break)


def _append_cell_heading(doc: Any, title: str, *, lead_break: bool) -> None:
    _append_body_paragraph(doc, title, _STYLE_CELL_HEADING, lead_break=lead_break)


def _append_section_heading(doc: Any, title: str) -> None:
    _append_body_paragraph(doc, title, _STYLE_SECTION_HEADING, lead_break=True)


def _insert_run_button_in_flow(
    doc: Any,
    *,
    cell_id: str,
    controls_before: int,
) -> None:
    """In-flow play control: runs ``notebook.run_cell.{hex}`` via protocol handler."""
    hex_id = cell_id_to_hex(cell_id)
    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.CommandButton")
    if model is None:
        raise RuntimeError("Failed to create form CommandButton")
    model.Name = f"nb_run_{hex_id}"
    model.Label = "\u25b6"
    if hasattr(model, "HelpText"):
        model.HelpText = _("Run code cell")
    model.ButtonType = _FORM_BUTTON_URL
    model.TargetURL = f"org.extension.writeragent:notebook.run_cell.{hex_id}"

    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape for run button")
    shape.setSize(Size(_RUN_BUTTON_SIZE, _RUN_BUTTON_SIZE))
    shape.Control = model
    shape.setPropertyValue("AnchorType", AS_CHARACTER)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    t_add = time.monotonic()
    text.insertTextContent(cursor, shape, False)
    _log_shape_add(
        step="run_button",
        name=model.Name,
        shapes_before=controls_before,
        create_ms=_mono_ms(t0),
        add_ms=_mono_ms(t_add),
        shape_h=_RUN_BUTTON_SIZE,
    )


def _insert_code_input_in_flow(
    doc: Any,
    *,
    name: str,
    source: str,
    controls_before: int,
) -> None:
    """Editable code cell: form TextField anchored in document flow at body end.

    Uses AS_CHARACTER + insertTextContent (same as forms.py Writer path). Without
    AnchorType, dp.add() on the draw page left controls inside the first heading
    and inflated page count (~1 soft page break per code cell).
    """
    display, truncated = _prepare_display_text(_coerce_notebook_text(source))
    raw_chars = len(source or "")

    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.TextField")
    if model is None:
        raise RuntimeError("Failed to create form TextField")
    model.Name = name
    if hasattr(model, "Label"):
        model.Label = "Code"
    if hasattr(model, "MultiLine"):
        model.MultiLine = True
    create_ms = _mono_ms(t0)

    t_text = time.monotonic()
    model.Text = display
    text_ms = _mono_ms(t_text)

    h = _height_for_text(display)
    t_shape = time.monotonic()
    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape")
    shape.setSize(Size(_DEFAULT_WIDTH, h))
    shape.Control = model
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    create_ms += _mono_ms(t_shape)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    t_add = time.monotonic()
    text.insertTextContent(cursor, shape, False)
    add_ms = _mono_ms(t_add)
    _log_shape_add(
        step="code_field",
        name=name,
        text_chars=raw_chars,
        truncated=truncated,
        shape_h=h,
        shapes_before=controls_before,
        create_ms=create_ms,
        text_ms=text_ms,
        add_ms=add_ms,
    )


def _cell_heading(idx: int, cell_type: str, execution_count: Any | None = None) -> str:
    if cell_type == "code":
        return f"{_format_in_prompt(execution_count)}\tCell {idx + 1}: Code"
    return f"Cell {idx + 1}: {cell_type.capitalize()}"


def import_ipynb_to_writer(doc: Any, path: str, ctx: Any | None = None) -> dict[str, Any]:
    """Read *path* (.ipynb): body text for markdown/raw/outputs; in-flow field for code."""
    run_t0 = time.monotonic()
    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = -1
    log.info("notebook import start path=%s file_size_bytes=%d", path, file_size)

    read_t0 = time.monotonic()
    nb = read_ipynb(path)
    cell_count = len(nb.cells)
    log.info("notebook import read_ipynb cells=%d read_ms=%d", cell_count, _mono_ms(read_t0))

    stats = {
        "cells": 0,
        "markdown": 0,
        "code": 0,
        "raw": 0,
        "shapes": 0,
        "images": 0,
        "outputs": 0,
        # Legacy key for dialog/tests
        "controls": 0,
    }

    notebook_in = _ensure_notebook_import_styles(doc)
    # Re-import replaces the whole registry (merge UX is Phase 3).
    registry_state = NotebookDocState(source_path=path)
    _import_cells(
        doc,
        nb,
        stats,
        cell_count,
        run_t0,
        ctx=ctx,
        notebook_in=notebook_in,
        registry_state=registry_state,
    )
    if registry_state.code_cells:
        from plugin.notebook.notebook_runner import init_registry_execution_counter

        init_registry_execution_counter(registry_state)
        save_registry(doc, registry_state)
        save_notebook_source_path(doc, path)
    flush_ui_idle(ctx)

    stats["controls"] = stats["shapes"]
    total_ms = _mono_ms(run_t0)
    log.info(
        "notebook import complete stats=%s total_ms=%d controls=%d avg_cell_ms=%d",
        stats,
        total_ms,
        stats["shapes"],
        total_ms // max(1, stats["cells"]),
    )
    return stats


def _import_cells(
    doc: Any,
    nb: Any,
    stats: dict[str, int],
    cell_count: int,
    run_t0: float,
    ctx: Any | None = None,
    *,
    notebook_in: str | None = None,
    registry_state: NotebookDocState | None = None,
) -> None:
    first_cell = True
    for idx, cell in enumerate(nb.cells):
        cell_t0 = time.monotonic()
        stats["cells"] += 1
        cell_type = getattr(cell, "cell_type", "raw")
        source = _coerce_notebook_text(getattr(cell, "source", "") or "")
        outputs = list(getattr(cell, "outputs", []) or []) if cell_type == "code" else []
        ec = getattr(cell, "execution_count", None) if cell_type == "code" else None

        log.debug(
            "notebook import cell start index=%d type=%s source_chars=%d output_count=%d controls=%d",
            idx,
            cell_type,
            len(source),
            len(outputs),
            stats["shapes"],
        )

        lead = not first_cell
        first_cell = False

        if cell_type == "code":
            title = _cell_heading(idx, cell_type, ec)
            _append_body_paragraph(doc, title, notebook_in, lead_break=lead)
        else:
            _append_cell_heading(doc, _cell_heading(idx, cell_type), lead_break=lead)

        if cell_type == "markdown":
            stats["markdown"] += 1
            _append_markdown_cell(doc, source, lead_break=True)
        elif cell_type == "code":
            stats["code"] += 1
            field_name = f"nb_cell_{idx}_code"
            if registry_state is not None:
                entry = new_code_cell_entry(idx, ec, field_name)
                registry_state.code_cells.append(entry)
            if registry_state is not None and registry_state.code_cells:
                entry = registry_state.code_cells[-1]
                _insert_run_button_in_flow(doc, cell_id=entry.cell_id, controls_before=stats["shapes"])
                stats["shapes"] += 1
            _insert_code_input_in_flow(
                doc,
                name=field_name,
                source=source,
                controls_before=stats["shapes"],
            )
            stats["shapes"] += 1
            _append_section_heading(doc, "Output")
            if registry_state is not None and registry_state.code_cells:
                bm_name = registry_state.code_cells[-1].output_start_bookmark
                insert_output_start_bookmark(doc, bm_name)
            out_text = _format_outputs_for_body(outputs, idx)
            if out_text.strip():
                stats["outputs"] += len([o for o in outputs if format_output_text(o).strip()])
                _append_body_text_block(doc, out_text, _STYLE_OUTPUT, lead_break=True)
            if _outputs_contain_image(outputs):
                images_added = _import_image_outputs_in_flow(doc, outputs, idx, images_before=stats["images"], ctx=ctx)
                stats["images"] += images_added
        else:
            stats["raw"] += 1
            _append_body_text_block(doc, source, _STYLE_BODY, lead_break=True)

        log.debug("notebook import cell done index=%d cell_ms=%d controls=%d", idx, _mono_ms(cell_t0), stats["shapes"])
        if (idx + 1) % _PROGRESS_EVERY_N_CELLS == 0 or idx + 1 == cell_count:
            log.info(
                "notebook import progress cell=%d/%d controls=%d elapsed_ms=%d",
                idx + 1,
                cell_count,
                stats["shapes"],
                _mono_ms(run_t0),
            )
