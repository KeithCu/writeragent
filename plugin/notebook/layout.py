# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Public layout vocabulary shared by notebook import, controls, and runner."""

from __future__ import annotations

import functools
import logging
import re
import time
from typing import Any
import unicodedata

from com.sun.star.awt import Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.framework.errors import best_effort
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_runtime_uid

log = logging.getLogger("writeragent.notebook")

STYLE_MD_H1 = "Heading 1"
STYLE_MD_H2 = "Heading 2"
STYLE_NOTEBOOK_IN = "WriterAgent Notebook In"
STYLE_NOTEBOOK_OUT = "WriterAgent Notebook Output"
PARAGRAPH_BREAK = 0  # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK

IN_PROMPT_RE = re.compile(r"^In \[[0-9 ]*\]:")
_MAX_IMPORT_TEXT_CHARS = 50_000
_TRUNCATION_SUFFIX = "\n\n[… truncated for import …]"

# com.sun.star.form.FormButtonType.PUSH — URL buttons open TargetURL via desktop, not our handler.
_FORM_BUTTON_PUSH = 0

RUN_PREFIX = "nb_run_"

# 1/100 mm — code field width falls back when page style is unavailable.
DEFAULT_WIDTH = 14000
MIN_FIELD_HEIGHT = 500
LINE_HEIGHT = 450
FIELD_HEIGHT_PAD = 280
MAX_FIELD_HEIGHT = 24000
_RUN_BUTTON_SIZE = 320
_SLOW_ADD_MS = 2000
CODE_FONT_NAME = "Liberation Mono"
_CODE_FONT_HEIGHT = 10
_CODE_FIELD_BG = 0xF7F7F7
_CODE_FIELD_BORDER = 0xD0D0D0


def coerce_notebook_text(value: Any) -> str:
    """Normalize nbformat text field (str or list of lines) to a single string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(line) for line in value)
    return str(value)


def prepare_display_text(text: str) -> str:
    """Display text only (markdown, raw, output), not code form fields."""
    display = text or ""
    if len(display) <= _MAX_IMPORT_TEXT_CHARS:
        return display
    keep = max(0, _MAX_IMPORT_TEXT_CHARS - len(_TRUNCATION_SUFFIX))
    return display[:keep] + _TRUNCATION_SUFFIX


def format_in_prompt(execution_count: Any | None) -> str:
    """Format Jupyter-style In [n]: gutter label for a code cell."""
    if execution_count is None:
        return "In [ ]:"
    return f"In [{execution_count}]:"


def mono_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def page_style(doc: Any | None) -> Any | None:
    """Page style of the first body paragraph, else Standard/Default.

    A cursor at the start of the body text carries the first paragraph's
    PageDescName; the document model does not have this property.
    File Open has no view yet, so this is not the view cursor.
    """
    if doc is None:
        return None

    def _get() -> Any | None:
        families = doc.getStyleFamilies().getByName("PageStyles")
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        name = str(cursor.getPropertyValue("PageDescName") or "")
        if name and families.hasByName(name):
            return families.getByName(name)
        for candidate in ("Standard", "Default", "Default Page Style"):
            if families.hasByName(candidate):
                return families.getByName(candidate)
        return None

    return best_effort(_get, "notebook controls page style lookup failed")


def text_width_from_style(style: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), falling back to DEFAULT_WIDTH."""
    if style is None:
        return DEFAULT_WIDTH

    def _calc() -> int:
        page_w = int(style.getPropertyValue("Width"))
        left = int(style.getPropertyValue("LeftMargin"))
        right = int(style.getPropertyValue("RightMargin"))
        return max(6000, page_w - left - right)

    val = best_effort(_calc, "notebook controls width from style failed")
    return val if val is not None else DEFAULT_WIDTH


def text_area_width_units(doc: Any | None) -> int:
    """Page width minus left/right margins (1/100 mm), for code fields and images."""
    style = page_style(doc)
    return text_width_from_style(style)


def cap_from_style(style: Any | None) -> int:
    """Cap AS_CHARACTER code fields at roughly body height, falling back to MAX_FIELD_HEIGHT."""
    if style is None:
        return MAX_FIELD_HEIGHT

    def _calc() -> int:
        page_h = int(style.getPropertyValue("Height"))
        top = int(style.getPropertyValue("TopMargin"))
        bottom = int(style.getPropertyValue("BottomMargin"))
        # Leave room for the In [n]: gutter on the same page when possible.
        return max(MIN_FIELD_HEIGHT, page_h - top - bottom - 1500)

    val = best_effort(_calc, "notebook controls cap from style failed")
    return val if val is not None else MAX_FIELD_HEIGHT


def _line_display_cols(line: str, *, max_cols: int | None = None) -> int:
    if line.endswith("\r"):
        line = line[:-1]
    line = line.expandtabs(4)
    cols = 0
    for ch in line:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            cols += 2
        else:
            cols += 1
        if max_cols is not None and cols >= max_cols:
            return cols
    return cols


def _raw_height_for_text(
    text: str,
    *,
    field_w: int | None = None,
    cap: int | None = None,
) -> int:
    if field_w is None:
        cols = 80
    else:
        char_width = int(_CODE_FONT_HEIGHT * 2540 * 0.6 / 72)
        cols = max(8, field_w // char_width)
    lines = 0
    base_pad = FIELD_HEIGHT_PAD + LINE_HEIGHT
    # Stop line walk once height strictly passes cap so raw_h > cap remains true.
    max_lines_needed = max(1, (cap - base_pad) // LINE_HEIGHT + 1) if cap is not None else None

    for line in (text or "").split("\n"):
        rem_lines = (max_lines_needed - lines) if max_lines_needed is not None else None
        max_cols = (rem_lines * cols) if rem_lines is not None and rem_lines > 0 else None
        cols_count = _line_display_cols(line, max_cols=max_cols)
        lines += max(1, (cols_count + cols - 1) // cols)
        if max_lines_needed is not None and lines >= max_lines_needed:
            break
    lines = max(1, lines)
    return lines * LINE_HEIGHT + base_pad


def height_for_text(text: str, doc: Any | None = None, *, field_w: int | None = None, cap: int | None = None) -> int:
    """Shape height in 1/100 mm so every source line is visible (no clipped last line)."""
    if field_w is None and doc is not None:
        field_w = text_area_width_units(doc)
    raw = _raw_height_for_text(text, field_w=field_w, cap=cap)
    if cap is None:
        cap = cap_from_style(page_style(doc))
    return max(MIN_FIELD_HEIGHT, min(cap, raw))


def _anchor_control_as_character(shape: Any) -> None:
    """In-flow control: AS_CHARACTER, top-aligned, no wrap beside the next shape."""
    shape.setPropertyValue("AnchorType", AS_CHARACTER)
    shape.setPropertyValue("VertOrient", 1)
    shape.setPropertyValue("TextWrap", 0)


def _set_model_props(model: Any, pairs: list[tuple[str, Any]]) -> None:
    """Set form-model properties querying getPropertySetInfo once. Skip names this model lacks.

    CommandButton has no ``Border``; the form TextField has no ``AutoVScroll``.
    A rejected value must not abort insertion of the code field.
    """
    info = best_effort(lambda: model.getPropertySetInfo(), "notebook controls getPropertySetInfo failed")
    if info is None:
        return
    for name, value in pairs:
        if info.hasPropertyByName(name):
            best_effort(functools.partial(model.setPropertyValue, name, value), f"notebook controls could not set model {name}")


def _style_code_field_model(model: Any, *, vscroll: bool = False) -> None:
    """Jupyter-like code box: light gray fill, hairline border, Liberation Mono."""
    _set_model_props(
        model,
        [
            ("MultiLine", True),
            ("FontName", CODE_FONT_NAME),
            ("FontHeight", _CODE_FONT_HEIGHT),
            ("BackgroundColor", _CODE_FIELD_BG),
            ("Border", 2),
            ("BorderColor", _CODE_FIELD_BORDER),
            ("VScroll", vscroll),
        ],
    )


def _style_run_button_model(model: Any) -> None:
    """Small ▶ without a fat 3D square around it."""
    _set_model_props(
        model,
        [
            ("Border", 0),
            ("BackgroundColor", 0xFFFFFF),
            ("FontHeight", 8),
            ("FocusOnClick", False),
            ("DefaultControl", "com.sun.star.form.control.CommandButton"),
        ],
    )


def log_shape_add(*, step: str, name: str = "", text_chars: int = 0, truncated: bool = False, shape_h: int = 0, shapes_before: int, create_ms: int = 0, text_ms: int = 0, add_ms: int = 0, ok: bool = True) -> None:
    total_ms = create_ms + text_ms + add_ms
    log.debug("notebook controls add step=%s name=%s text_chars=%d truncated=%s shape_h=%d shapes_before=%d create_ms=%d text_ms=%d add_ms=%d ok=%s", step, name, text_chars, truncated, shape_h, shapes_before, create_ms, text_ms, add_ms, ok)
    if total_ms >= _SLOW_ADD_MS:
        log.warning("notebook controls slow UNO add step=%s total_ms=%d shapes_before=%d", step, total_ms, shapes_before)


def insert_run_button_in_flow(doc: Any, *, cell_id: str, controls_before: int) -> None:
    """In-flow ▶ on the ``In [n]:`` gutter paragraph (not the tall gray field)."""
    hex_id = cell_id.replace("-", "")
    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.CommandButton")
    if model is None:
        raise RuntimeError("Failed to create form CommandButton")
    model.Name = f"{RUN_PREFIX}{hex_id}"
    model.Label = "\u25b6"
    if hasattr(model, "HelpText"):
        model.HelpText = _("Run code cell")
    model.ButtonType = _FORM_BUTTON_PUSH
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
    log_shape_add(step="run_button", name=model.Name, shapes_before=controls_before, create_ms=mono_ms(t0), add_ms=mono_ms(t_add), shape_h=_RUN_BUTTON_SIZE)


def insert_code_input_in_flow(
    doc: Any,
    *,
    name: str,
    source: str,
    controls_before: int,
    field_w: int | None = None,
    cap: int | None = None,
) -> None:
    """Editable code cell: form TextField anchored in document flow at body end."""
    source_text = coerce_notebook_text(source)
    raw_chars = len(source_text)

    if field_w is None:
        field_w = text_area_width_units(doc)
    if cap is None:
        cap = cap_from_style(page_style(doc))

    raw_h = _raw_height_for_text(source_text, field_w=field_w, cap=cap)
    h = max(MIN_FIELD_HEIGHT, min(cap, raw_h))
    vscroll = raw_h > cap

    t0 = time.monotonic()
    model = doc.createInstance("com.sun.star.form.component.TextField")
    if model is None:
        raise RuntimeError("Failed to create form TextField")
    model.Name = name
    if hasattr(model, "Label"):
        model.Label = _("Code")
    _style_code_field_model(model, vscroll=vscroll)
    create_ms = mono_ms(t0)

    t_text = time.monotonic()
    # Full source is the runnable program; display cap is for body text only.
    # MaxTextLen defaults to 0 (unlimited); do not set it or non-zero caps at 32k.
    model.Text = source_text
    text_ms = mono_ms(t_text)

    t_shape = time.monotonic()
    shape = doc.createInstance("com.sun.star.drawing.ControlShape")
    if shape is None:
        raise RuntimeError("Failed to create ControlShape")
    shape.setSize(Size(field_w, h))
    shape.Control = model
    _anchor_control_as_character(shape)
    create_ms += mono_ms(t_shape)

    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    t_add = time.monotonic()
    text.insertTextContent(cursor, shape, False)
    add_ms = mono_ms(t_add)
    log_shape_add(step="code_field", name=name, text_chars=raw_chars, truncated=False, shape_h=h, shapes_before=controls_before, create_ms=create_ms, text_ms=text_ms, add_ms=add_ms)


def doc_key(doc: Any) -> str:
    """Stable id for listener de-dupe across Python wrappers of the same document.

    Untitled Writer docs have an empty URL, so ``id(doc)`` was used. Import
    and bootstrap ``install_notebook_run_button_wiring`` can wire on
    different PyUNO wrappers of the same document — one ▶ click then ran
    the cell multiple times (``[In [4]]`` jumped to ``[In [7]]``).
    ``RuntimeUID`` is the same object for every wrapper of that document.

    Read it with ``get_runtime_uid``.
    """
    uid = get_runtime_uid(doc)
    if uid:
        return f"uid:{uid}"
    return f"id:{id(doc)}"


__all__ = [
    "CODE_FONT_NAME",
    "DEFAULT_WIDTH",
    "FIELD_HEIGHT_PAD",
    "IN_PROMPT_RE",
    "LINE_HEIGHT",
    "MAX_FIELD_HEIGHT",
    "MIN_FIELD_HEIGHT",
    "PARAGRAPH_BREAK",
    "RUN_PREFIX",
    "STYLE_MD_H1",
    "STYLE_MD_H2",
    "STYLE_NOTEBOOK_IN",
    "STYLE_NOTEBOOK_OUT",
    "cap_from_style",
    "coerce_notebook_text",
    "doc_key",
    "format_in_prompt",
    "height_for_text",
    "insert_code_input_in_flow",
    "insert_run_button_in_flow",
    "log_shape_add",
    "mono_ms",
    "page_style",
    "prepare_display_text",
    "text_area_width_units",
    "text_width_from_style",
]
