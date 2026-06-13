# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_fs."""

from __future__ import annotations

import zipfile
from pathlib import Path

from plugin.embeddings import embeddings_fs


def test_content_hash_stable():
    assert embeddings_fs.content_hash("  hello  ") == embeddings_fs.content_hash("hello")


def test_extract_writer_paragraphs_from_odt(tmp_path: Path):
    odt = tmp_path / "doc.odt"
    content_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text>
    <text:p>First paragraph</text:p>
    <text:p>   </text:p>
    <text:p>Second</text:p>
  </office:text></office:body>
</office:document-content>"""
    with zipfile.ZipFile(odt, "w") as zf:
        zf.writestr("content.xml", content_xml)
    texts = embeddings_fs.extract_writer_paragraphs(str(odt))
    assert texts == ["First paragraph", "Second"]


def test_extract_writer_paragraphs_fodt(tmp_path: Path):
    fodt = tmp_path / "doc.fodt"
    fodt.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<text:p>Flat body</text:p>
</office:document>""",
        encoding="utf-8",
    )
    assert embeddings_fs.extract_writer_paragraphs(str(fodt)) == ["Flat body"]


def test_paragraph_chunks_from_path(tmp_path: Path):
    odt = tmp_path / "a.odt"
    content_xml = b"""<?xml version="1.0"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:text><text:p>Body</text:p></office:text></office:body>
</office:document-content>"""
    with zipfile.ZipFile(odt, "w") as zf:
        zf.writestr("content.xml", content_xml)
    chunks = embeddings_fs.paragraph_chunks_from_path(str(odt))
    assert len(chunks) == 1
    assert chunks[0].text == "Body"
    assert chunks[0].para_index == 0
    assert chunks[0].doc_url.startswith("file:")


def test_guess_indexable_paths_includes_ods_and_office(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "budget.xlsx").write_bytes(b"placeholder")
    odt = tmp_path / "doc.odt"
    content_xml = b"""<?xml version="1.0"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:text><text:p>x</text:p></office:text></office:body>
</office:document-content>"""
    with zipfile.ZipFile(odt, "w") as zf:
        zf.writestr("content.xml", content_xml)
    (tmp_path / "Budget.ods").write_bytes(b"placeholder")
    (tmp_path / "deck.odp").write_bytes(b"placeholder")
    entries = embeddings_fs.guess_indexable_paths(str(tmp_path))
    names = sorted(entry.name for entry in entries)
    assert names == ["Budget.ods", "budget.xlsx", "deck.odp", "doc.odt", "notes.txt"]


def test_all_indexable_extensions_includes_foreign():
    assert ".xlsx" in embeddings_fs.ALL_INDEXABLE_EXTENSIONS
    assert ".docx" in embeddings_fs.ALL_INDEXABLE_EXTENSIONS
    assert ".pptx" in embeddings_fs.ALL_INDEXABLE_EXTENSIONS
    assert ".pdf" not in embeddings_fs.ALL_INDEXABLE_EXTENSIONS


def test_paragraph_chunks_from_txt(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("Alpha paragraph", encoding="utf-8")
    chunks = embeddings_fs.paragraph_chunks_from_path(str(path))
    assert len(chunks) == 1
    assert chunks[0].text == "Alpha paragraph"
    assert chunks[0].doc_url.endswith("/notes.txt")


def test_guess_writer_paths_alias(tmp_path: Path):
    (tmp_path / "doc.odt").write_bytes(b"x")
    assert embeddings_fs.guess_writer_paths(str(tmp_path)) == embeddings_fs.guess_indexable_paths(str(tmp_path))
