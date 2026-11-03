# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ODP/ODG fixtures for embeddings / FTS tests."""

from __future__ import annotations

from pathlib import Path

from odf.draw import Frame, Page, TextBox
from odf.opendocument import OpenDocumentDrawing, OpenDocumentPresentation
from odf.presentation import Notes
from odf.text import P


def _add_page_text(page: Page, *, body: str, notes: str = "", spans: list[str] | None = None) -> None:
    body_frame = Frame(width="10cm", height="3cm")
    body_box = TextBox()
    p = P(text=body)
    if spans:
        from odf.text import Span
        for span_text in spans:
            p.addElement(Span(text=span_text))
    body_box.addElement(p)
    body_frame.addElement(body_box)
    page.addElement(body_frame)
    if notes:
        notes_el = Notes()
        note_frame = Frame(width="10cm", height="3cm")
        note_box = TextBox()
        note_box.addElement(P(text=notes))
        note_frame.addElement(note_box)
        notes_el.addElement(note_frame)
        page.addElement(notes_el)


def write_deck_odp(path: Path, *, body: str = "Q4 Revenue", notes: str = "", spans: list[str] | None = None) -> None:
    """Minimal Impress .odp with one slide for extract and FTS tests."""
    doc = OpenDocumentPresentation()
    page = Page(name="Intro", masterpagename="Default", stylename="dp1")
    _add_page_text(page, body=body, notes=notes, spans=spans)
    doc.presentation.addElement(page)
    doc.save(str(path))


def write_drawing_odg(path: Path, *, body: str = "Diagram label") -> None:
    """Minimal Draw .odg with one page."""
    doc = OpenDocumentDrawing()
    page = Page(name="Layer1", masterpagename="Default", stylename="dp1")
    _add_page_text(page, body=body)
    doc.drawing.addElement(page)
    doc.save(str(path))
