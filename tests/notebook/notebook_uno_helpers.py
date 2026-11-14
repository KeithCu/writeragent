# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared UNO document-inspection helpers for notebook UNO tests."""

from __future__ import annotations

from plugin.framework.uno_context import enum_has_more


def paragraphs(doc) -> list[tuple[str, str]]:
    """Return ``(ParaStyleName, text)`` pairs for every Writer paragraph in *doc*."""
    out: list[tuple[str, str]] = []
    enum = doc.getText().createEnumeration()
    while enum_has_more(enum):
        el = enum.nextElement()
        try:
            if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                continue
            style = str(el.getPropertyValue("ParaStyleName") or "")
            text = str(el.getString() or "")
        except Exception:
            continue
        out.append((style, text))
    return out


def draw_control_names(doc) -> list[str]:
    """Return the control ``Name`` of every ControlShape on *doc*'s draw page."""
    names: list[str] = []
    dp = doc.getDrawPage()
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        try:
            if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
                continue
            name = str(getattr(shape.Control, "Name", "") or "")
        except Exception:
            continue
        if name:
            names.append(name)
    return names


def anchor_paragraph_string(doc, shape_name: str) -> str:
    """Return the full text of the paragraph anchoring *shape_name*, or ``""``."""
    from plugin.notebook.form_lookup import find_control_shape_by_name

    shape = find_control_shape_by_name(doc, shape_name)
    if shape is None:
        return ""
    try:
        text = doc.getText()
        cursor = text.createTextCursorByRange(shape.getAnchor())
        cursor.gotoStartOfParagraph(False)
        cursor.gotoEndOfParagraph(True)
        return str(cursor.getString() or "")
    except Exception:
        return ""


def output_text_for_cell(doc, cell) -> str:
    """Return the rendered output text between *cell*'s bookmark and the next cell boundary."""
    from plugin.notebook.layout import STYLE_NOTEBOOK_IN
    from plugin.notebook.notebook_runner import (
        _cursor_after_bookmark,
        _is_next_cell_boundary,
        _paragraph_string,
    )
    from plugin.notebook.writer_importer import resolve_para_style

    start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if start is None:
        return ""
    text = doc.getText()
    notebook_in = resolve_para_style(doc, STYLE_NOTEBOOK_IN)
    end = text.createTextCursorByRange(start)
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(end.ParaStyleName, _paragraph_string(end), notebook_in):
            end.gotoStartOfParagraph(False)
            break
    else:
        end.gotoEnd(False)
    sel = text.createTextCursorByRange(start)
    sel.gotoRange(end.getStart(), True)
    return str(sel.getString() or "")
