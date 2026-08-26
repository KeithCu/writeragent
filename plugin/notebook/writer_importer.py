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
import html as html_lib
import logging
import os
import re
import struct
import tempfile
import time
from types import SimpleNamespace
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

# 1/100 mm — code field width falls back when page style is unavailable.
_DEFAULT_WIDTH = 14000
_MIN_FIELD_HEIGHT = 500
_LINE_HEIGHT = 450
# Hairline + descenders. One extra _LINE_HEIGHT of wrap slack so a leftover
# wrap (In[3] `type(a2))`) is fully inside the gray box. Half-line slack
# (`_LINE_HEIGHT // 2`) clipped that last visual line mid-glyph and did not
# tighten short cells (In[1] still had empty gray). Live with a bit of empty
# gray on short cells. No wrap-width calculator, no HScroll.
_FIELD_HEIGHT_PAD = 280
_WRAP_SLACK = _LINE_HEIGHT
# AS_CHARACTER cannot split; cap near one page body so a huge cell page-breaks as a
# unit. Do not use a 9 cm cap — that sliced 15-line cells.
_MAX_FIELD_HEIGHT = 24000
_STACK_MARGIN_X = 5000
_STACK_GAP = 400
_STACK_INITIAL_BOTTOM = 800
# Small gutter ▶ — a 6 mm bordered square sat inside the first code line.
_RUN_BUTTON_SIZE = 320
_PROGRESS_EVERY_N_CELLS = 10
_SLOW_ADD_MS = 2000
_MAX_IMPORT_TEXT_CHARS = 50_000
_TRUNCATION_SUFFIX = "\n\n[… truncated for import …]"
_MAX_OUTPUTS_PER_CELL = 200
_MAX_IMAGE_DECODE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_DISPLAY_WIDTH_MM = 170
_DEFAULT_IMAGE_HEIGHT_MM = 80
_IMAGE_MIME_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/svg+xml": ".svg"}
_CODE_FONT_NAME = "Liberation Mono"
_CODE_FONT_HEIGHT = 10
_CODE_FIELD_BG = 0xF7F7F7
_CODE_FIELD_BORDER = 0xD0D0D0
_NOTEBOOK_IN_CHAR_COLOR = 0x307FC1
_HTTP_IMAGE_TIMEOUT_SEC = 2

# Writer paragraph styles (document locale usually provides these English names).
# Markdown ATX uses Heading 1/2. Cell chrome (Heading 3 "Cell N: …" / Heading 4
# "Output") is no longer written — Jupyter has neither.
_STYLE_MD_H1 = "Heading 1"
_STYLE_MD_H2 = "Heading 2"
_STYLE_CELL_HEADING = "Heading 3"
_STYLE_SECTION_HEADING = "Heading 4"
_STYLE_OUTPUT = "Preformatted Text"
_STYLE_BODY = "Text Body"

# Auto-created on import for Jupyter-like In [n]: gutter (1/100 mm margins).
_STYLE_NOTEBOOK_IN = "WriterAgent Notebook In"
_NOTEBOOK_IN_CHAR_HEIGHT = 9
_NOTEBOOK_IN_MARGIN_TOP = 200
_NOTEBOOK_IN_MARGIN_BOTTOM = 40

_PARAGRAPH_BREAK = 0  # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_HTML_TAG_RE = re.compile(r"<\s*[a-zA-Z]", re.DOTALL)
# CommonMark ATX: 1–6 hashes, space, title, optional closing hashes.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# Negative lookbehind so ``![alt](src)`` is not treated as a markdown link.
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
_MD_UL_RE = re.compile(r"^[*+-][ \t]+(.*)$")
_MD_OL_RE = re.compile(r"^\d+[.)][ \t]+(.*)$")
_MD_IMAGE_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_HTML_IMG_RE = re.compile(r"(?is)<img\b[^>]*?/?>")
_HTML_A_RE = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")
_HTML_A_OR_IMG_TAG_RE = re.compile(r"(?is)</?(?:img|a)\b[^>]*?/?>")
_HTML_ATTR_RE = re.compile(
    r"""(?is)([a-z_:][-a-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)
_IN_PROMPT_RE = re.compile(r"^In \[[0-9 ]*\]:")


def _mono_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class _ImportStackCursor:  # pyright: ignore[reportUnusedClass]  # notebook import stack; used by importer tests
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


def _height_for_text(text: str, doc: Any | None = None) -> int:
    """Shape height in 1/100 mm so every source line is visible (no clipped last line)."""
    lines = 0
    for line in (text or "").split("\n"):
        lines += max(1, (len(line) + 79) // 80)
    lines = max(1, lines)
    raw = lines * _LINE_HEIGHT + _FIELD_HEIGHT_PAD + _WRAP_SLACK
    cap = _max_field_height_units(doc)
    return max(_MIN_FIELD_HEIGHT, min(cap, raw))


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


def format_output_text(output: Any, execution_count: Any | None = None) -> str:
    """Turn one nbformat output object into plain text for the document body.

    Stream/print text has no ``Out [n]:`` prefix (Jupyter puts that only on
    ``execute_result`` / last-line values).
    """
    output_type = getattr(output, "output_type", None) or output.get("output_type", "")
    if output_type == "stream":
        text = _coerce_notebook_text(getattr(output, "text", None) or output.get("text", ""))
        return text
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
                if output_type == "execute_result" and execution_count is not None:
                    return f"Out [{execution_count}]: {plain}"
                return plain
            mime_types = ", ".join(sorted(data.keys()))
            return f"[non-text output: {mime_types}]"
    return str(output)


def format_all_outputs(outputs: list[Any]) -> str:
    parts = [format_output_text(o) for o in (outputs or [])]
    return "\n\n".join(p for p in parts if p.strip())


def _format_outputs_for_body(outputs: list[Any], cell_index: int, execution_count: Any | None = None) -> str:
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
        text = format_output_text(output, execution_count)
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


def _jpeg_pixel_size(raw: bytes) -> tuple[int, int] | None:
    """Read SOF width/height so JPEG plots keep aspect ratio when capped."""
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(raw) - 8:
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", raw[i + 5 : i + 9])
            if w >= 1 and h >= 1:
                return w, h
            return None
        if marker == 0xD9 or marker == 0xDA:
            return None
        length = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        i += 2 + length
    return None


def _svg_pixel_size(raw: bytes) -> tuple[int, int] | None:
    """Read width/height or viewBox so SVG badges are not stretched to the page cap."""
    text = raw.decode("utf-8", errors="ignore")[:4000]
    vb = re.search(
        r"viewBox\s*=\s*[\"']?\s*[-0-9.]+\s+[-0-9.]+\s+([0-9.]+)\s+([0-9.]+)",
        text,
        flags=re.IGNORECASE,
    )
    if vb:
        w, h = float(vb.group(1)), float(vb.group(2))
        if w >= 1 and h >= 1:
            return int(w), int(h)
    wm = re.search(r"\bwidth\s*=\s*[\"']?([0-9.]+)", text, flags=re.IGNORECASE)
    hm = re.search(r"\bheight\s*=\s*[\"']?([0-9.]+)", text, flags=re.IGNORECASE)
    if wm and hm:
        w, h = float(wm.group(1)), float(hm.group(1))
        if w >= 1 and h >= 1:
            return int(w), int(h)
    return None


def _image_mime_from_bytes(raw: bytes, path: str) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(raw) >= 2 and raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    lower = (path or "").lower()
    head = raw.lstrip()[:256].lower()
    if lower.endswith(".svg") or b"<svg" in head:
        return "image/svg+xml"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "image/png"


def _text_area_width_units(doc: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), for code fields and images."""
    if doc is None:
        return _DEFAULT_WIDTH
    try:
        families = doc.getStyleFamilies().getByName("PageStyles")
        name = ""
        try:
            name = str(doc.getPropertyValue("PageDescName") or "")
        except Exception:
            name = ""
        style = None
        if name and families.hasByName(name):
            style = families.getByName(name)
        else:
            for candidate in ("Standard", "Default", "Default Page Style"):
                if families.hasByName(candidate):
                    style = families.getByName(candidate)
                    break
        if style is None:
            return _DEFAULT_WIDTH
        page_w = int(style.getPropertyValue("Width"))
        left = int(style.getPropertyValue("LeftMargin"))
        right = int(style.getPropertyValue("RightMargin"))
        return max(6000, page_w - left - right)
    except Exception:
        log.debug("notebook import could not read page text-area width", exc_info=True)
        return _DEFAULT_WIDTH


def _max_field_height_units(doc: Any | None) -> int:
    """Cap AS_CHARACTER code fields at roughly the page body height."""
    if doc is None:
        return _MAX_FIELD_HEIGHT
    try:
        families = doc.getStyleFamilies().getByName("PageStyles")
        name = ""
        try:
            name = str(doc.getPropertyValue("PageDescName") or "")
        except Exception:
            name = ""
        style = None
        if name and families.hasByName(name):
            style = families.getByName(name)
        else:
            for candidate in ("Standard", "Default", "Default Page Style"):
                if families.hasByName(candidate):
                    style = families.getByName(candidate)
                    break
        if style is None:
            return _MAX_FIELD_HEIGHT
        page_h = int(style.getPropertyValue("Height"))
        top = int(style.getPropertyValue("TopMargin"))
        bottom = int(style.getPropertyValue("BottomMargin"))
        # Leave room for the In [n]: gutter on the same page when possible.
        return max(_MIN_FIELD_HEIGHT, page_h - top - bottom - 1500)
    except Exception:
        log.debug("notebook import could not read page text-area height", exc_info=True)
        return _MAX_FIELD_HEIGHT


def _display_size_units(raw: bytes, mime: str, *, max_width_mm: float | None = None) -> tuple[int, int]:
    """Map decoded image bytes to Writer size in 1/100 mm (capped width, keep aspect)."""
    cap_mm = float(max_width_mm) if max_width_mm and max_width_mm > 0 else float(_MAX_IMAGE_DISPLAY_WIDTH_MM)
    px_size = None
    if mime == "image/png":
        px_size = _png_pixel_size(raw)
    elif mime in ("image/jpeg", "image/jpg"):
        px_size = _jpeg_pixel_size(raw)
    elif mime == "image/svg+xml":
        px_size = _svg_pixel_size(raw)
    if px_size is not None:
        px_w, px_h = px_size
    else:
        px_w, px_h = None, None
    if px_w and px_h:
        w_mm = px_w * 25.4 / 96
        h_mm = px_h * 25.4 / 96
        if w_mm > cap_mm:
            scale = cap_mm / w_mm
            w_mm = cap_mm
            h_mm = h_mm * scale
        return _mm_to_units(w_mm, h_mm)
    return _mm_to_units(cap_mm, _DEFAULT_IMAGE_HEIGHT_MM)


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


def _apply_notebook_image_flow(graphic: Any) -> None:
    """AS_CHARACTER already in-flow; pin wrap off so plots do not float beside text."""
    for name, val in (("TextWrap", 0), ("SurroundContour", False)):
        try:
            graphic.setPropertyValue(name, val)
        except Exception:
            log.debug("notebook image wrap property %s not applied", name, exc_info=True)


def _insert_image_in_flow(
    doc: Any,
    *,
    raw: bytes,
    mime: str,
    images_before: int,
    ctx: Any | None = None,
    text_cursor: Any | None = None,
) -> bool:
    """Embed notebook image output in document flow (TextGraphicObject).

    Import appends at body end. Live ▶ must pass *text_cursor* under the cell —
    ``gotoEnd`` was dumping matplotlib plots at the document end and jumping the view.
    """
    suffix = _IMAGE_MIME_SUFFIX.get(mime, ".png")
    tmp_path = None
    t0 = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        max_w_mm = _text_area_width_units(doc) / 100.0
        w_units, h_units = _display_size_units(raw, mime, max_width_mm=max_w_mm)
        w_mm = w_units / 100.0
        h_mm = h_units / 100.0
        text = doc.getText()
        if text_cursor is not None:
            cursor = text_cursor
        else:
            cursor = text.createTextCursor()
            cursor.gotoEnd(False)
        t_add = time.monotonic()
        graphic = None
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
            graphic = image
        if graphic is not None:
            _apply_notebook_image_flow(graphic)
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


def flush_ui_idle(ctx: Any | None, *, log_phase: str | None = None) -> None:
    if ctx is None:
        return
    t0 = time.monotonic()
    try:
        from plugin.framework.uno_context import process_events_to_idle

        process_events_to_idle(ctx)
    except Exception:
        log.debug("processEventsToIdle failed", exc_info=True)
        return
    if log_phase:
        log.info("notebook import %s elapsed_ms=%d", log_phase, _mono_ms(t0))


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


def _no_spellcheck_locale() -> Any:
    """Locale that disables Writer spell/grammar checking (ISO 639-2 ``zxx``)."""
    try:
        import uno
    except ImportError:
        # External test/development Pythons can see LibreOffice's uno.py but fail to
        # load pyuno when the bundled Python ABI differs. A namespace with the same
        # attributes is enough for setPropertyValue mocks and keeps import tests portable.
        return SimpleNamespace(Language="zxx", Country="")

    create = getattr(uno, "createUnoStruct", None)
    if callable(create):
        loc: Any = create("com.sun.star.lang.Locale")
        loc.Language = "zxx"
        loc.Country = ""
        return loc
    # pytest may load a mocked ``uno`` without ``createUnoStruct``; attrs still reach setPropertyValue.
    return SimpleNamespace(Language="zxx", Country="")


def _apply_no_spellcheck_for_import(doc: Any) -> None:
    """Notebook bodies are code/markdown — suppress spellcheck for imported content."""
    loc = _no_spellcheck_locale()
    for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
        try:
            doc.setPropertyValue(prop, loc)
        except Exception:
            log.debug("notebook import could not set document %s", prop, exc_info=True)
    para_styles = _get_para_styles(doc)
    if para_styles is None:
        return
    for style_name in (
        "Standard",
        _STYLE_BODY,
        _STYLE_MD_H1,
        _STYLE_MD_H2,
        _STYLE_CELL_HEADING,
        _STYLE_SECTION_HEADING,
        _STYLE_OUTPUT,
        _STYLE_NOTEBOOK_IN,
    ):
        resolved = _resolve_para_style(doc, style_name)
        if not resolved:
            continue
        try:
            if not para_styles.hasByName(resolved):
                continue
            style = para_styles.getByName(resolved)
        except Exception:
            log.debug("notebook import could not open style %r", resolved, exc_info=True)
            continue
        for prop in ("CharLocale", "CharLocaleAsian", "CharLocaleComplex"):
            try:
                style.setPropertyValue(prop, loc)
            except Exception:
                log.debug("notebook import could not set %s on %r", prop, resolved, exc_info=True)


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
    """Create notebook In [n]: gutter style once per document; return resolved name.

    Parent is Text Body, not Heading 3. Heading 3 as parent inherited 14 pt and
    large before/after spacing, which made gutters look like report headings and
    opened half-empty pages between cells.
    """
    para_styles = _get_para_styles(doc)
    if para_styles is None:
        return None
    parent_body = _resolve_para_style(doc, _STYLE_BODY) or "Text Body"

    no_lang = _no_spellcheck_locale()
    property_updates: dict[str, Any] = {
        "ParaAdjust": 0,
        "ParaLeftMargin": -1270,  # Out-dented by 1/2 inch (12.7 mm)
        "ParaTopMargin": _NOTEBOOK_IN_MARGIN_TOP,
        "ParaBottomMargin": _NOTEBOOK_IN_MARGIN_BOTTOM,
        "ParaKeepTogether": True,
        "ParaKeepWithNext": True,
        "CharHeight": _NOTEBOOK_IN_CHAR_HEIGHT,
        "CharWeight": 150,
        "CharColor": _NOTEBOOK_IN_CHAR_COLOR,
        "CharLocale": no_lang,
        "CharLocaleAsian": no_lang,
        "CharLocaleComplex": no_lang,
    }

    _create_import_para_style(
        doc,
        para_styles,
        _STYLE_NOTEBOOK_IN,
        parent_style=parent_body,
        property_updates=property_updates,
    )
    return _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)


def _format_in_prompt(execution_count: Any | None) -> str:
    if execution_count is None:
        return "In [ ]:"
    return f"In [{execution_count}]:"


def _html_attr(tag_or_attrs: str, name: str) -> str:
    """Read one HTML attribute value (quoted or bare) from a start tag or attr blob."""
    want = name.lower()
    for match in _HTML_ATTR_RE.finditer(tag_or_attrs or ""):
        if (match.group(1) or "").lower() != want:
            continue
        raw = match.group(2)
        if raw is None:
            raw = match.group(3)
        if raw is None:
            raw = match.group(4)
        return html_lib.unescape((raw or "").strip())
    return ""


def _html_img_and_a_to_markdown(source: str) -> str:
    """Turn Jupyter HTML ``<img>`` / ``<a>`` into markdown so mixed cells stay CommonMark.

    Sending the whole cell through StarWriter HTML wrote a temp file whose directory
    was the resolver for relative ``<img src>``, so ``../images/*.png`` next to the
    ``.ipynb`` 404'd. Headings and ``**bold**`` in those cells were also left raw.
    """
    if not source or "<" not in source:
        return source

    def repl_img(match: re.Match[str]) -> str:
        src = _html_attr(match.group(0), "src")
        if not src:
            return ""
        alt = _html_attr(match.group(0), "alt")
        return f"![{alt}]({src})"

    work = _HTML_IMG_RE.sub(repl_img, source)

    def repl_a(match: re.Match[str]) -> str:
        href = _html_attr(match.group(1), "href")
        inner = match.group(2) or ""
        if not href:
            return inner
        stripped = inner.strip()
        # Linked image (Colab badge): keep the image; ``[![alt](src)](url)`` is not
        # an embed in ``_iter_markdown_blocks``.
        if _MD_IMAGE_LINE_RE.match(stripped) or (stripped.startswith("![") and "](" in stripped):
            return inner
        text = re.sub(r"\s+", " ", stripped)
        if not text:
            return inner
        return f"[{text}]({href})"

    return _HTML_A_RE.sub(repl_a, work)


def _looks_like_html(text: str) -> bool:
    """True when the cell still has HTML other than ``<img>`` / ``<a>``.

    Those two tags are converted to markdown first; treating them as a raw HTML
    cell dumped ``##`` / ``**`` as literal text and broke relative images.
    """
    stripped = _HTML_A_OR_IMG_TAG_RE.sub(" ", text or "")
    return bool(_HTML_TAG_RE.search(stripped.strip()))


def _inline_backticks_to_html(text: str) -> str:
    """Escape *text* and wrap `` `code` `` spans in ``<code>``."""
    parts: list[str] = []
    last = 0
    for match in _INLINE_CODE_RE.finditer(text):
        parts.append(html_lib.escape(text[last : match.start()]))
        parts.append(f"<code>{html_lib.escape(match.group(1))}</code>")
        last = match.end()
    parts.append(html_lib.escape(text[last:]))
    return "".join(parts)


def _inline_markdown_to_html(text: str) -> str:
    """Escape *text* and wrap ``code``, **bold**, *italic*, and ``[text](url)`` as HTML."""
    if (
        not _BOLD_RE.search(text or "")
        and not _ITALIC_RE.search(text or "")
        and not _MD_IMAGE_RE.search(text or "")
        and not _MD_LINK_RE.search(text or "")
    ):
        return _inline_backticks_to_html(text)
    placeholders: list[str] = []

    def hold(html: str) -> str:
        placeholders.append(html)
        return f"\x00H{len(placeholders) - 1}\x00"

    def escape_non_placeholders(s: str) -> str:
        parts: list[str] = []
        last = 0
        for m in re.finditer(r"\x00H\d+\x00", s):
            parts.append(html_lib.escape(s[last : m.start()]))
            parts.append(m.group(0))
            last = m.end()
        parts.append(html_lib.escape(s[last:]))
        return "".join(parts)

    work = text or ""
    work = _MD_IMAGE_RE.sub("", work)

    def stash_code(match: re.Match[str]) -> str:
        return hold(f"<code>{html_lib.escape(match.group(1))}</code>")

    def stash_bold(match: re.Match[str]) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        escaped_inner = escape_non_placeholders(inner or "")
        return hold(f"<strong>{escaped_inner}</strong>")

    def stash_italic(match: re.Match[str]) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        escaped_inner = escape_non_placeholders(inner or "")
        return hold(f"<em>{escaped_inner}</em>")

    def stash_link(match: re.Match[str]) -> str:
        inner = match.group(1) or ""
        url = _split_markdown_image_src(match.group(2) or "")
        if not url:
            return match.group(0)
        href = html_lib.escape(url, quote=True)
        return hold(f'<a href="{href}">{escape_non_placeholders(inner)}</a>')

    work = _INLINE_CODE_RE.sub(stash_code, work)
    work = _MD_LINK_RE.sub(stash_link, work)
    work = _BOLD_RE.sub(stash_bold, work)
    work = _ITALIC_RE.sub(stash_italic, work)
    escaped = escape_non_placeholders(work)

    def restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    result = escaped
    while "\x00H" in result:
        result = re.sub(r"\x00H(\d+)\x00", restore, result)
    return result


def _paragraph_needs_html(text: str) -> bool:
    if "`" in (text or ""):
        return True
    if "**" in text or "__" in text:
        return True
    if _MD_IMAGE_RE.search(text):
        return True
    if _MD_LINK_RE.search(text):
        return True
    if _ITALIC_RE.search(text):
        return True
    return False


def _iter_markdown_blocks(source: str) -> list[tuple[str, Any]]:
    """Split CommonMark source into ATX headings, lists, images, and paragraphs.

    Not a full CommonMark parser: GFM tables stay as body text. ``#`` → h1,
    ``##`` and deeper → h2.
    """
    blocks: list[tuple[str, Any]] = []
    para: list[str] = []
    list_kind: str | None = None
    list_items: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(("p", "\n".join(para)))
            para.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_kind and list_items:
            blocks.append((list_kind, list(list_items)))
        list_items.clear()
        list_kind = None

    for raw_line in (source or "").splitlines():
        line = raw_line.rstrip()
        match = _ATX_HEADING_RE.match(line)
        if match:
            flush_para()
            flush_list()
            title = (match.group(2) or "").strip()
            if title:
                kind = "h1" if len(match.group(1)) <= 1 else "h2"
                blocks.append((kind, title))
            continue
        img_line = _MD_IMAGE_LINE_RE.match(line.strip())
        if img_line:
            flush_para()
            flush_list()
            blocks.append(("img", (img_line.group(1) or "", img_line.group(2).strip())))
            continue
        ul_match = _MD_UL_RE.match(line)
        if ul_match:
            flush_para()
            if list_kind != "ul":
                flush_list()
                list_kind = "ul"
            list_items.append(ul_match.group(1))
            continue
        ol_match = _MD_OL_RE.match(line)
        if ol_match:
            flush_para()
            if list_kind != "ol":
                flush_list()
                list_kind = "ol"
            list_items.append(ol_match.group(1))
            continue
        if not line.strip():
            flush_para()
            flush_list()
            continue
        flush_list()
        para.append(line)
    flush_para()
    flush_list()
    return blocks


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


def _append_paragraph_break_at_end(doc: Any) -> None:
    """Insert a paragraph break at body end so following in-flow shapes are not in the previous para."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)


def _split_markdown_image_src(raw: str) -> str:
    src = (raw or "").strip()
    if not src:
        return ""
    if src[0] in "\"'":
        return src
    return src.split()[0].strip("<>")


def _fetch_remote_image(url: str) -> str | None:
    """Download a reachable http(s) image to a temp file; None when not reachable."""
    try:
        from urllib.request import Request, urlopen

        req = Request(url, headers={"User-Agent": "WriterAgent-notebook-import"})
        with urlopen(req, timeout=_HTTP_IMAGE_TIMEOUT_SEC) as resp:
            data = resp.read(_MAX_IMAGE_DECODE_BYTES + 1)
        if not data or len(data) > _MAX_IMAGE_DECODE_BYTES:
            return None
        suffix = ".png"
        lower = url.lower().split("?", 1)[0]
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            if lower.endswith(ext):
                suffix = ".jpg" if ext == ".jpeg" else ext
                break
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(data)
        finally:
            tmp.close()
        return tmp.name
    except Exception:
        log.debug("notebook markdown image URL not reachable: %s", url, exc_info=True)
        return None


def _resolve_markdown_image_path(src: str, notebook_dir: str | None) -> str | None:
    """Return a local filesystem path when *src* is a reachable image."""
    src = _split_markdown_image_src(src)
    if not src:
        return None
    if src.startswith("file:"):
        try:
            import uno

            src = str(uno.fileUrlToSystemPath(src))
        except Exception:
            src = src.replace("file://", "", 1)
    if src.startswith("http://") or src.startswith("https://"):
        return _fetch_remote_image(src)
    if os.path.isfile(src):
        return src
    if notebook_dir:
        joined = os.path.normpath(os.path.join(notebook_dir, src))
        if os.path.isfile(joined):
            return joined
    return None


def _embed_markdown_image(doc: Any, src: str, notebook_dir: str | None, *, ctx: Any | None = None) -> bool:
    path = _resolve_markdown_image_path(src, notebook_dir)
    if not path:
        return False
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_IMAGE_DECODE_BYTES + 1)
        if not raw or len(raw) > _MAX_IMAGE_DECODE_BYTES:
            return False
        mime = _image_mime_from_bytes(raw, path)
        _append_paragraph_break_at_end(doc) if _doc_body_nonempty(doc) else None
        return _insert_image_in_flow(doc, raw=raw, mime=mime, images_before=0, ctx=ctx)
    except Exception:
        log.debug("notebook markdown image embed failed path=%s", path, exc_info=True)
        return False
    finally:
        if src.startswith("http://") or src.startswith("https://"):
            try:
                if path and path.startswith(tempfile.gettempdir()) and os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass


def _unglue_last_paragraph(doc: Any) -> None:
    """Clear KeepWithNext on the last body paragraph.

    Built-in Heading 2 (and some HTML list styles) keep-with-next. That glues the
    last markdown paragraph onto the following code cell's unsplittable
    AS_CHARACTER field, so Writer moves the whole block and leaves a page hole.
    Call this immediately before inserting a code cell. KeepWithNext on the
    ``In [n]:`` gutter still holds the label with its field.
    """
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        cursor.setPropertyValue("ParaKeepWithNext", False)
        cursor.setPropertyValue("ParaKeepTogether", False)
    except Exception:
        log.debug("notebook import unglue last paragraph failed", exc_info=True)


def _anchor_control_as_character(shape: Any) -> None:
    """In-flow control: AS_CHARACTER, top-aligned, no wrap beside the next shape."""
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    # VertOrientation.TOP — keep ▶ and the field on one line box (max height,
    # not stacked). CHAR_CENTER / wrap was a plausible source of blank top bands.
    try:
        shape.setPropertyValue("VertOrient", 1)
    except Exception:
        log.debug("notebook import VertOrient not applied", exc_info=True)
    try:
        shape.setPropertyValue("TextWrap", 0)
    except Exception:
        log.debug("notebook import TextWrap not applied", exc_info=True)


def _append_body_paragraph(
    doc: Any,
    content: str,
    para_style: str | None,
    *,
    lead_break: bool,
    keep_with_next: bool = False,
) -> None:
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
    if keep_with_next:
        for prop, val in (("ParaKeepTogether", True), ("ParaKeepWithNext", True)):
            try:
                cursor.setPropertyValue(prop, val)
            except Exception:
                log.debug("notebook import %s not applied", prop, exc_info=True)
    text.insertString(cursor, content, False)


def _append_body_text_block(
    doc: Any,
    block: str,
    para_style: str | None,
    *,
    lead_break: bool = True,
) -> None:
    """Append one paragraph; internal newlines stay in the same block."""
    display, _unused = _prepare_display_text(block)
    if not display:
        return
    _append_body_paragraph(doc, display, para_style, lead_break=lead_break)


def _insert_html_at_body_end(doc: Any, html: str, *, lead_break: bool) -> bool:
    """Insert an HTML fragment at the document end. Returns False on failure."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    if lead_break and _doc_body_nonempty(doc):
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        cursor.gotoEnd(False)
    from plugin.writer.html_import import insert_html_fragment_at_cursor

    try:
        insert_html_fragment_at_cursor(cursor, _wrap_html_fragment(html), wrap=False)
        return True
    except Exception:
        log.exception("notebook import HTML insert failed; falling back to plain text")
        return False


def _append_markdown_cell(
    doc: Any,
    source: str,
    *,
    lead_break: bool,
    notebook_dir: str | None = None,
    ctx: Any | None = None,
) -> None:
    """Markdown cell: ATX headings, lists, bold/italic, ``[text](url)``, images.

    HTML ``<img>`` / ``<a>`` become markdown first so mixed cells still render
    headings and lists. Whole-cell StarWriter HTML is only for leftover HTML
    (tables, divs). Relative ``<img src>`` resolves against the ``.ipynb``
    directory via ``_embed_markdown_image``, not a temp-file HTML import.
    No ``Cell N: Markdown`` chrome.
    """
    display, _unused = _prepare_display_text(source)
    if not display:
        return
    display = _html_img_and_a_to_markdown(display)
    if _looks_like_html(display):
        if not _insert_html_at_body_end(doc, display, lead_break=lead_break):
            _append_body_paragraph(doc, display, _STYLE_BODY, lead_break=False)
        return
    first = True
    for kind, payload in _iter_markdown_blocks(display):
        block_lead = lead_break if first else True
        first = False
        if kind == "h1":
            _append_body_paragraph(doc, str(payload), _STYLE_MD_H1, lead_break=block_lead)
        elif kind == "h2":
            _append_body_paragraph(doc, str(payload), _STYLE_MD_H2, lead_break=block_lead)
        elif kind in ("ul", "ol"):
            items = payload if isinstance(payload, list) else [str(payload)]
            lis = "".join(f"<li>{_inline_markdown_to_html(str(item))}</li>" for item in items)
            html = f"<{kind}>{lis}</{kind}>"
            if not _insert_html_at_body_end(doc, html, lead_break=block_lead):
                for i, item in enumerate(items):
                    prefix = "• " if kind == "ul" else f"{i + 1}. "
                    _append_body_paragraph(
                        doc, prefix + str(item), _STYLE_BODY, lead_break=block_lead if i == 0 else True
                    )
        elif kind == "img":
            alt, src = payload if isinstance(payload, tuple) else ("", str(payload))
            if not _embed_markdown_image(doc, str(src), notebook_dir, ctx=ctx):
                fallback = alt or src
                _append_body_paragraph(doc, str(fallback), _STYLE_BODY, lead_break=block_lead)
        elif _paragraph_needs_html(str(payload)):
            html = f"<p>{_inline_markdown_to_html(str(payload))}</p>"
            if not _insert_html_at_body_end(doc, html, lead_break=block_lead):
                _append_body_paragraph(doc, str(payload), _STYLE_BODY, lead_break=False)
        else:
            _append_body_paragraph(doc, str(payload), _STYLE_BODY, lead_break=block_lead)


def _set_model_prop(model: Any, name: str, value: Any) -> None:
    try:
        model.setPropertyValue(name, value)
        return
    except Exception:
        pass
    try:
        setattr(model, name, value)
    except Exception:
        log.debug("notebook import could not set model %s", name, exc_info=True)


def _style_code_field_model(model: Any) -> None:
    """Jupyter-like code box: light gray fill, hairline border, Liberation Mono."""
    _set_model_prop(model, "MultiLine", True)
    _set_model_prop(model, "FontName", _CODE_FONT_NAME)
    _set_model_prop(model, "FontHeight", _CODE_FONT_HEIGHT)
    _set_model_prop(model, "BackgroundColor", _CODE_FIELD_BG)
    # UnoControlEditModel: 0 none, 1 3D, 2 simple — simple + gray is a hairline.
    _set_model_prop(model, "Border", 2)
    _set_model_prop(model, "BorderColor", _CODE_FIELD_BORDER)
    _set_model_prop(model, "VScroll", False)
    _set_model_prop(model, "AutoVScroll", False)


def _style_run_button_model(model: Any) -> None:
    """Small ▶ without a fat 3D square around it."""
    _set_model_prop(model, "Border", 0)
    _set_model_prop(model, "BackgroundColor", 0xFFFFFF)
    _set_model_prop(model, "FontHeight", 8)
    _set_model_prop(model, "FocusOnClick", False)
    _set_model_prop(model, "DefaultControl", "com.sun.star.form.control.CommandButton")


def _style_control_paragraph(doc: Any) -> None:
    """Field-only paragraph: full text-area width, no hanging indent for ▶."""
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        body = _resolve_para_style(doc, _STYLE_BODY)
        if body:
            cursor.setPropertyValue("ParaStyleName", body)
        cursor.setPropertyValue("ParaFirstLineIndent", 0)
        cursor.setPropertyValue("ParaLeftMargin", 0)
        cursor.setPropertyValue("ParaTopMargin", 0)
        cursor.setPropertyValue("ParaBottomMargin", 150)
        # KeepTogether on a one-line para with a tall AS_CHARACTER object can
        # force a page break even when the box would still fit. The shape
        # cannot split either way; do not add an extra keep.
        cursor.setPropertyValue("ParaKeepTogether", False)
        # Do not KeepWithNext: gluing the tall AS_CHARACTER field to following
        # markdown pulled both onto the next page and left a half-empty page.
        cursor.setPropertyValue("ParaKeepWithNext", False)
    except Exception:
        log.debug("notebook import control paragraph style failed", exc_info=True)


def _insert_run_button_in_flow(
    doc: Any,
    *,
    cell_id: str,
    controls_before: int,
    ctx: Any | None = None,
) -> None:
    """In-flow ▶ on the ``In [n]:`` gutter paragraph (not the tall gray field)."""
    from plugin.notebook.notebook_controls import form_button_push_type

    hex_id = cell_id_to_hex(cell_id)
    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.CommandButton")
    if model is None:
        raise RuntimeError("Failed to create form CommandButton")
    model.Name = f"nb_run_{hex_id}"
    model.Label = "\u25b6"
    if hasattr(model, "HelpText"):
        model.HelpText = _("Run code cell")
    # URL-type buttons open TargetURL via desktop and do not reach our ProtocolHandler.
    model.ButtonType = form_button_push_type()
    _style_run_button_model(model)

    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape for run button")
    shape.setSize(Size(_RUN_BUTTON_SIZE, _RUN_BUTTON_SIZE))
    shape.Control = model
    _anchor_control_as_character(shape)

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
    _style_code_field_model(model)
    create_ms = _mono_ms(t0)

    t_text = time.monotonic()
    model.Text = display
    text_ms = _mono_ms(t_text)

    h = _height_for_text(display, doc)
    field_w = _text_area_width_units(doc)
    t_shape = time.monotonic()
    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape")
    shape.setSize(Size(field_w, h))
    shape.Control = model
    _anchor_control_as_character(shape)
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
    """Code-cell gutter prompt only. Markdown/raw have no Cell N chrome."""
    if cell_type == "code":
        return _format_in_prompt(execution_count)
    return ""


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

    _apply_no_spellcheck_for_import(doc)

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
    cells_t0 = time.monotonic()
    _import_cells(
        doc,
        nb,
        stats,
        cell_count,
        run_t0,
        ctx=ctx,
        notebook_in=notebook_in,
        registry_state=registry_state,
        notebook_dir=os.path.dirname(os.path.abspath(path)) if path else None,
    )
    log.info("notebook import cells_done elapsed_ms=%d cells=%d", _mono_ms(cells_t0), stats["cells"])
    if registry_state.code_cells:
        from plugin.notebook.notebook_controls import ensure_form_design_mode_off, wire_all_notebook_run_buttons
        from plugin.notebook.notebook_runner import init_registry_execution_counter

        reg_t0 = time.monotonic()
        init_registry_execution_counter(registry_state)
        save_registry(doc, registry_state)
        save_notebook_source_path(doc, path)
        ensure_form_design_mode_off(doc)
        log.info(
            "notebook import registry_and_design_mode elapsed_ms=%d code_cells=%d",
            _mono_ms(reg_t0),
            len(registry_state.code_cells),
        )
        flush_ui_idle(ctx, log_phase="flush_ui_idle after_registry")
        if ctx is not None:
            wire_all_notebook_run_buttons(ctx, doc)
    else:
        flush_ui_idle(ctx, log_phase="flush_ui_idle after_cells")

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
    notebook_dir: str | None = None,
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
            # Previous markdown (Heading 2 keep-with-next, HTML lists) must not
            # glue onto this cell's unsplittable field.
            _unglue_last_paragraph(doc)
            title = _cell_heading(idx, cell_type, ec)
            _append_body_paragraph(doc, title, notebook_in, lead_break=lead, keep_with_next=True)
            field_name = f"nb_cell_{idx}_code"
            if registry_state is not None:
                entry = new_code_cell_entry(idx, ec, field_name)
                registry_state.code_cells.append(entry)
                # ▶ on the In [n]: row so it is not AS_CHARACTER-stacked under the
                # tall field. update_in_prompt rewrites leading Text only (stops at Frame).
                _insert_run_button_in_flow(
                    doc,
                    cell_id=entry.cell_id,
                    controls_before=stats["shapes"],
                    ctx=ctx,
                )
                stats["shapes"] += 1
            _append_paragraph_break_at_end(doc)
            _style_control_paragraph(doc)

        if cell_type == "markdown":
            stats["markdown"] += 1
            _append_markdown_cell(
                doc, source, lead_break=lead, notebook_dir=notebook_dir, ctx=ctx
            )
        elif cell_type == "code":
            stats["code"] += 1
            field_name = f"nb_cell_{idx}_code"
            _insert_code_input_in_flow(
                doc,
                name=field_name,
                source=source,
                controls_before=stats["shapes"],
            )
            stats["shapes"] += 1
            # Invisible output bookmark at the end of the field paragraph — not a
            # visible "Output" heading. A bookmark inside "Output" leaked as "/" .
            if registry_state is not None and registry_state.code_cells:
                bm_name = registry_state.code_cells[-1].output_start_bookmark
                insert_output_start_bookmark(doc, bm_name)
            out_text = _format_outputs_for_body(outputs, idx, execution_count=ec)
            if out_text.strip():
                stats["outputs"] += len(
                    [o for o in outputs if format_output_text(o, ec).strip()]
                )
                _append_body_text_block(doc, out_text, _STYLE_OUTPUT, lead_break=True)
            if _outputs_contain_image(outputs):
                _append_paragraph_break_at_end(doc)
                images_added = _import_image_outputs_in_flow(
                    doc, outputs, idx, images_before=stats["images"], ctx=ctx
                )
                stats["images"] += images_added
        else:
            stats["raw"] += 1
            _append_body_text_block(doc, source, _STYLE_BODY, lead_break=lead)

        log.debug("notebook import cell done index=%d cell_ms=%d controls=%d", idx, _mono_ms(cell_t0), stats["shapes"])
        if (idx + 1) % _PROGRESS_EVERY_N_CELLS == 0 or idx + 1 == cell_count:
            log.info(
                "notebook import progress cell=%d/%d controls=%d elapsed_ms=%d",
                idx + 1,
                cell_count,
                stats["shapes"],
                _mono_ms(run_t0),
            )
