# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_ooxml_extract."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from plugin.embeddings.venv import embeddings_ooxml_extract as ooxml


def test_extract_csv_rows(tmp_path: Path):
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n\n", encoding="utf-8")
    assert ooxml.extract_csv_rows(str(path)) == ["a\tb", "1\t2"]


def test_extract_plaintext_paragraphs_blank_lines(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("First block\n\nSecond block\n", encoding="utf-8")
    assert ooxml.extract_plaintext_paragraphs(str(path)) == ["First block", "Second block"]


def test_extract_plaintext_paragraphs_lines(tmp_path: Path):
    path = tmp_path / "lines.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    assert ooxml.extract_plaintext_paragraphs(str(path)) == ["alpha", "beta"]


def test_extract_rtf_paragraphs(tmp_path: Path):
    path = tmp_path / "doc.rtf"
    path.write_text(r"{\rtf1 hello \par world}", encoding="utf-8")
    assert ooxml.extract_rtf_paragraphs(str(path)) == ["hello", "world"]


def test_extract_pptx_passages(tmp_path: Path):
    slide_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>"""
    pptx = tmp_path / "deck.pptx"
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    passages = ooxml.extract_pptx_passages(str(pptx))
    assert passages == ["[Slide: Slide1]\tSlide text"]


def test_extract_pptx_passages_order_and_notes(tmp_path: Path):
    pptx = tmp_path / "deck2.pptx"
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr("ppt/presentation.xml", '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<p:sldIdLst>
<p:sldId id="256" r:id="rId2"/>
<p:sldId id="257" r:id="rId3"/>
</p:sldIdLst>
</p:presentation>''')

        zf.writestr("ppt/_rels/presentation.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide10.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
</Relationships>''')

        zf.writestr("ppt/slides/slide10.xml", '''<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 1 text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>''')

        zf.writestr("ppt/slides/slide2.xml", '''<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 2 text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>''')

        zf.writestr("ppt/slides/_rels/slide10.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/>
</Relationships>''')
        zf.writestr("ppt/slides/_rels/slide2.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide2.xml"/>
</Relationships>''')

        zf.writestr("ppt/notesSlides/notesSlide1.xml", '''<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 1 notes</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>''')

        zf.writestr("ppt/notesSlides/notesSlide2.xml", '''<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 2 notes</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>''')

    passages = ooxml.extract_pptx_passages(str(pptx))
    assert passages == [
        "[Slide: Slide1]\tSlide 1 text",
        "[Notes: Slide1]\tSlide 1 notes",
        "[Slide: Slide2]\tSlide 2 text",
        "[Notes: Slide2]\tSlide 2 notes"
    ]


def test_extract_docx_paragraphs_uses_python_docx(tmp_path: Path):
    pytest.importorskip("docx")
    from docx import Document

    path = tmp_path / "file.docx"
    document = Document()
    document.add_paragraph("Hello")
    document.add_paragraph("")
    document.add_paragraph("World")
    document.save(path)
    assert ooxml.extract_docx_paragraphs(str(path)) == ["Hello", "World"]


def test_extract_spreadsheet_rows_xlsx(tmp_path: Path):
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    path = tmp_path / "book.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws["A1"] = "Revenue"
    ws["B1"] = 100
    wb.save(path)
    rows = ooxml.extract_spreadsheet_rows(str(path))
    assert rows == ["[Sheet: Budget]\tRevenue\t100"]
