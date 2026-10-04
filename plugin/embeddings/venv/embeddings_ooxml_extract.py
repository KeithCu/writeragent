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
import zipfile
from pathlib import Path

import defusedxml.ElementTree as ET

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
    except ImportError as exc:
        log.debug("python-docx not installed — docx extract skipped for %s", path, exc_info=True)
        raise RuntimeError(f"python-docx not installed — docx extract skipped for {path}") from exc
    try:
        document = Document(path)
    except Exception as exc:
        log.debug("extract_docx_paragraphs failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_docx_paragraphs failed for {path}") from exc
    passages: list[str] = []
    for paragraph in document.paragraphs:
        try:
            # Extract all text elements, including those nested in w:hyperlink
            texts = [node.text for node in paragraph._element.xpath(".//w:t") if node.text]
            text = "".join(texts).strip()
        except Exception:
            text = str(paragraph.text or "").strip()
        if text:
            passages.append(text)
    return passages


def extract_spreadsheet_rows(path: str) -> list[str] | None:
    """One passage per non-empty row from .xlsx/.xls (pandas + openpyxl/xlrd). Returns None on failure."""
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
        return None
    try:
        sheets = pd.read_excel(path, engine=engine, sheet_name=None, header=None)
    except ImportError:
        log.debug("%s engine not installed — spreadsheet extract skipped for %s", engine, path, exc_info=True)
        return None
    except Exception:
        log.debug("extract_spreadsheet_rows failed for %s", path, exc_info=True)
        return None

    rows: list[str] = []
    for sheet_name, frame in sheets.items():
        for _unused, row in frame.iterrows():
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
    except OSError as exc:
        log.debug("extract_csv_rows failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_csv_rows failed for {path}") from exc
    return rows


def extract_plaintext_paragraphs(path: str) -> list[str]:
    """Plain .txt: blank-line paragraphs, else one passage per non-empty line."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("extract_plaintext_paragraphs failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_plaintext_paragraphs failed for {path}") from exc
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
    except OSError as exc:
        log.debug("extract_rtf_paragraphs failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_rtf_paragraphs failed for {path}") from exc
    text = raw.replace("\\par", "\n").replace("\\line", "\n")
    text = _RTF_CONTROL.sub("", text)
    text = text.replace("{", "").replace("}", "")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _texts_from_ooxml_slide_xml(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return ""
    paras: list[str] = []
    for p_node in root.iter(f"{_DRAWML_NS}p"):
        parts: list[str] = []
        for t_node in p_node.iter(f"{_DRAWML_NS}t"):
            if t_node.text:
                parts.append(t_node.text)
        para_text = "".join(parts).strip()
        if para_text:
            paras.append(para_text)
    return "\n".join(paras)


def extract_pptx_passages(path: str) -> list[str]:
    """Slide body + speaker notes from .pptx (stdlib zip + DrawingML text nodes)."""
    import posixpath
    passages: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            namelist = zf.namelist()

            slide_seq = []
            if "ppt/presentation.xml" in namelist:
                try:
                    pres_xml = zf.read("ppt/presentation.xml")
                    root = ET.fromstring(pres_xml)
                    for sldId in root.findall(".//{http://schemas.openxmlformats.org/presentationml/2006/main}sldId"):
                        rid = sldId.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                        if rid:
                            slide_seq.append(rid)
                except Exception:
                    pass

            rid_to_slide = {}
            if "ppt/_rels/presentation.xml.rels" in namelist:
                try:
                    rels_xml = zf.read("ppt/_rels/presentation.xml.rels")
                    root = ET.fromstring(rels_xml)
                    for rel in root.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                        if rel.get("Type", "").endswith("/slide"):
                            target = rel.get("Target")
                            if target:
                                if target.startswith("/"):
                                    target = target[1:]
                                elif not target.startswith("ppt/"):
                                    target = "ppt/" + target
                                rid_to_slide[rel.get("Id")] = target
                except Exception:
                    pass

            slide_names = []
            for rid in slide_seq:
                if rid in rid_to_slide and rid_to_slide[rid] in namelist:
                    slide_names.append(rid_to_slide[rid])

            if not slide_names:
                def _slide_num(n: str) -> int:
                    m = re.search(r'slide(\d+)\.xml$', n)
                    return int(m.group(1)) if m else 999999
                slide_names = sorted(
                    (name for name in namelist if name.startswith("ppt/slides/slide") and name.endswith(".xml")),
                    key=_slide_num
                )

            for index, name in enumerate(slide_names, start=1):
                body = _texts_from_ooxml_slide_xml(zf.read(name))
                if body:
                    passages.append(f"[Slide: Slide{index}]\t{body}")

                base = posixpath.basename(name)
                dirname = posixpath.dirname(name)
                slide_rel_path = posixpath.join(dirname, "_rels", base + ".rels")
                if slide_rel_path in namelist:
                    try:
                        slide_rels_xml = zf.read(slide_rel_path)
                        sroot = ET.fromstring(slide_rels_xml)
                        for rel in sroot.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                            if rel.get("Type", "").endswith("/notesSlide"):
                                ntarget = rel.get("Target")
                                if ntarget:
                                    if ntarget.startswith("/"):
                                        notes_path = ntarget[1:]
                                    else:
                                        notes_path = posixpath.normpath(posixpath.join(dirname, ntarget))
                                    if notes_path in namelist:
                                        notes = _texts_from_ooxml_slide_xml(zf.read(notes_path))
                                        if notes:
                                            passages.append(f"[Notes: Slide{index}]\t{notes}")
                    except Exception:
                        pass
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        log.debug("extract_pptx_passages failed for %s", path, exc_info=True)
        raise RuntimeError(f"extract_pptx_passages failed for {path}") from exc
    return passages
