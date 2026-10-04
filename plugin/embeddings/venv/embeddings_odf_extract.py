# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv Impress/Draw page and Calc row extract for folder embeddings / FTS (odfpy + pandas)."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["extract_draw_pages", "extract_calc_rows"]


# --- Impress/Draw (.odp/.odg) extract helpers ---

def _paragraph_plain_text(paragraph: Any) -> str:
    from odf.teletype import extractText
    if paragraph is not None:
        return extractText(paragraph).strip()
    return ""


def _is_descendant_of(element: Any, ancestor: Any) -> bool:
    parent = getattr(element, "parentNode", None)
    while parent is not None:
        if parent is ancestor:
            return True
        parent = getattr(parent, "parentNode", None)
    return False


def _collect_paragraph_text(page: Any, *, notes_type: Any) -> str:
    from odf.text import P

    notes_nodes = page.getElementsByType(notes_type)
    texts: list[str] = []
    for paragraph in page.getElementsByType(P):
        if any(_is_descendant_of(paragraph, notes_node) for notes_node in notes_nodes):
            continue
        text = _paragraph_plain_text(paragraph)
        if text:
            texts.append(text)
    return "\n".join(texts)


def _collect_notes_text(page: Any, *, notes_type: Any) -> str:
    from odf.text import P

    texts: list[str] = []
    for notes_node in page.getElementsByType(notes_type):
        for paragraph in notes_node.getElementsByType(P):
            text = _paragraph_plain_text(paragraph)
            if text:
                texts.append(text)
    return "\n".join(texts)


def extract_draw_pages(path: str) -> list[str]:
    """Read indexable passages from Impress/Draw .odp/.odg (one slide/page body + optional notes)."""
    try:
        from odf.draw import Page as DrawPage
        from odf.opendocument import load
        from odf.presentation import Notes
    except ImportError as exc:
        log.debug("odfpy not installed — ODP/ODG extract skipped for %s", path, exc_info=True)
        raise RuntimeError(f"odfpy not installed — ODP/ODG extract skipped for {path}") from exc

    try:
        document = load(path)
    except Exception as exc:
        log.debug("extract_draw_pages failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_draw_pages failed for {path}") from exc

    passages: list[str] = []
    page_index = 0
    for page in document.getElementsByType(DrawPage):
        page_index += 1
        name = str(page.getAttribute("name") or "").strip() or f"Page{page_index}"
        body = _collect_paragraph_text(page, notes_type=Notes)
        if body:
            passages.append(f"[Slide: {name}]\t{body}")
        notes = _collect_notes_text(page, notes_type=Notes)
        if notes:
            passages.append(f"[Notes: {name}]\t{notes}")
    return passages


# --- Calc (.ods) extract ---

def extract_calc_rows(path: str) -> list[str] | None:
    """Read indexable row text from a Calc .ods/.ots/.fods (one passage per non-empty row). Returns None on failure."""
    try:
        import pandas as pd
    except ImportError:
        log.debug("pandas not installed — ODS extract skipped for %s", path, exc_info=True)
        return None
    try:
        sheets = pd.read_excel(path, engine="odf", sheet_name=None, header=None)
    except ImportError:
        log.debug("odfpy not installed — ODS extract skipped for %s", path, exc_info=True)
        return None
    except Exception:
        log.debug("extract_calc_rows failed for %s", path, exc_info=True)
        return None

    rows: list[str] = []
    for sheet_name, frame in sheets.items():
        for _unused, row in frame.iterrows():
            cells = [str(value).strip() for value in row if pd.notna(value) and str(value).strip()]
            if cells:
                rows.append(f"[Sheet: {sheet_name}]\t" + "\t".join(cells))
    return rows
