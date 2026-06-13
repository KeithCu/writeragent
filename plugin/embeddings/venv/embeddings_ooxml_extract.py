# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv extract for Microsoft Office / plain-text siblings (no UNO).

Requires pip packages in the embeddings venv: python-docx, openpyxl, xlrd (see
``EMBEDDINGS_VENV_PIP_INSTALL``). PDF is intentionally out of scope.
"""
from __future__ import annotations

import csv
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "extract_csv_rows",
    "extract_docx_paragraphs",
    "extract_plaintext_paragraphs",
    "extract_pptx_passages",
    "extract_rtf_paragraphs",
    "extract_spreadsheet_rows",
]

_DRAWML_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def extract_docx_paragraphs(path: str) -> list[str]:
    """Body paragraphs from a .docx file (python-docx)."""
    try:
        from docx import Document
    except ImportError:
        log.debug("python-docx not installed — docx extract skipped for %s", path, exc_info=True)
        return []
    try:
        document = Document(path)
    except Exception:
        log.debug("extract_docx_paragraphs failed for %s", path, exc_info=True)
        return []
    passages: list[str] = []
    for paragraph in document.paragraphs:
        text = str(paragraph.text or "").strip()
        if text:
            passages.append(text)
    return passages


def extract_spreadsheet_rows(path: str) -> list[str]:
    """One passage per non-empty row from .xlsx/.xls (pandas + openpyxl/xlrd)."""
    ext = Path(path).suffix.lower()
    if ext == ".xlsx":
        engine = "openpyxl"
    elif ext == ".xls":
        engine = "xlrd"
    else:
        return []
    try:
        import pandas as pd
    except ImportError:
        log.debug("pandas not installed — spreadsheet extract skipped for %s", path, exc_info=True)
        return []
    try:
        sheets = pd.read_excel(path, engine=engine, sheet_name=None, header=None)
    except ImportError:
        log.debug("%s engine not installed — spreadsheet extract skipped for %s", engine, path, exc_info=True)
        return []
    except Exception:
        log.debug("extract_spreadsheet_rows failed for %s", path, exc_info=True)
        return []

    rows: list[str] = []
    for sheet_name, frame in sheets.items():
        for _, row in frame.iterrows():
            cells = [str(value).strip() for value in row if pd.notna(value) and str(value).strip()]
            if cells:
                rows.append(f"[Sheet: {sheet_name}]\t" + "\t".join(cells))
    return rows


def extract_csv_rows(path: str) -> list[str]:
    """One passage per non-empty CSV row (tab-joined cells)."""
    rows: list[str] = []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.reader(handle):
                cells = [cell.strip() for cell in row if str(cell).strip()]
                if cells:
                    rows.append("\t".join(cells))
    except OSError:
        log.debug("extract_csv_rows failed for %s", path, exc_info=True)
    return rows


def extract_plaintext_paragraphs(path: str) -> list[str]:
    """Plain .txt: blank-line paragraphs, else one passage per non-empty line."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.debug("extract_plaintext_paragraphs failed for %s", path, exc_info=True)
        return []
    parts = [part.strip() for part in text.split("\n\n") if part.strip()]
    if len(parts) > 1:
        return parts
    if len(parts) == 1 and "\n" in parts[0]:
        return [line.strip() for line in parts[0].splitlines() if line.strip()]
    if parts:
        return parts
    return [line.strip() for line in text.splitlines() if line.strip()]


_RTF_CONTROL = re.compile(r"\\([a-z]+-?\d* ?|[{}])")


def extract_rtf_paragraphs(path: str) -> list[str]:
    """Best-effort RTF paragraph text for cross-file routing (not a full RTF parser)."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.debug("extract_rtf_paragraphs failed for %s", path, exc_info=True)
        return []
    text = raw.replace("\\par", "\n").replace("\\line", "\n")
    text = _RTF_CONTROL.sub("", text)
    text = text.replace("{", "").replace("}", "")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _texts_from_ooxml_slide_xml(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    parts: list[str] = []
    for node in root.iter(f"{_DRAWML_NS}t"):
        if node.text and node.text.strip():
            parts.append(node.text.strip())
    return " ".join(parts)


def extract_pptx_passages(path: str) -> list[str]:
    """Slide body + speaker notes from .pptx (stdlib zip + DrawingML text nodes)."""
    passages: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            slide_names = sorted(
                name for name in zf.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            for index, name in enumerate(slide_names, start=1):
                body = _texts_from_ooxml_slide_xml(zf.read(name))
                if body:
                    passages.append(f"[Slide: Slide{index}]\t{body}")
            note_names = sorted(
                name
                for name in zf.namelist()
                if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
            )
            for index, name in enumerate(note_names, start=1):
                notes = _texts_from_ooxml_slide_xml(zf.read(name))
                if notes:
                    passages.append(f"[Notes: Slide{index}]\t{notes}")
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        log.debug("extract_pptx_passages failed for %s", path, exc_info=True)
    return passages
