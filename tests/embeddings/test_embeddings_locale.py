# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_locale."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from plugin.embeddings import embeddings_locale as loc_mod

ODF_STYLES_DE = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
  <office:styles>
    <style:default-style style:family="paragraph">
      <style:text-properties fo:language="de" fo:country="DE"/>
    </style:default-style>
  </office:styles>
</office:document-styles>"""

ODF_META_FR = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
  <office:meta><dc:language>fr-FR</dc:language></office:meta>
</office:document-meta>"""

DOCX_CORE_EN = b"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:language>en-GB</dc:language>
</cp:coreProperties>"""


def _write_odt(path: Path, *, styles: bytes | None = None, meta: bytes | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        if styles is not None:
            zf.writestr("styles.xml", styles)
        if meta is not None:
            zf.writestr("meta.xml", meta)


def test_odf_locale_from_styles_xml(tmp_path: Path) -> None:
    odt = tmp_path / "german.odt"
    _write_odt(odt, styles=ODF_STYLES_DE)
    assert loc_mod.resolve_document_locale_bcp47(str(odt)) == "de-DE"


def test_odf_locale_meta_fallback_when_styles_missing(tmp_path: Path) -> None:
    odt = tmp_path / "french.odt"
    _write_odt(odt, meta=ODF_META_FR)
    assert loc_mod.resolve_document_locale_bcp47(str(odt)) == "fr-FR"


def test_odf_styles_preferred_over_meta(tmp_path: Path) -> None:
    odt = tmp_path / "mixed.odt"
    _write_odt(odt, styles=ODF_STYLES_DE, meta=ODF_META_FR)
    assert loc_mod.resolve_document_locale_bcp47(str(odt)) == "de-DE"


def test_docx_locale_from_core_xml(tmp_path: Path) -> None:
    docx = tmp_path / "english.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("docProps/core.xml", DOCX_CORE_EN)
    assert loc_mod.resolve_document_locale_bcp47(str(docx)) == "en-GB"


def test_resolve_skips_non_prose_extension(tmp_path: Path) -> None:
    ods = tmp_path / "sheet.ods"
    ods.write_bytes(b"not odf")
    assert loc_mod.resolve_document_locale_bcp47(str(ods)) is None


def test_langdetect_from_body_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loc_mod, "_langdetect_from_sample", lambda _sample: "de-DE")
    assert loc_mod.resolve_document_locale_bcp47("/tmp/notes.txt", body_sample="Guten Tag.") == "de-DE"


def test_langdetect_on_txt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Das ist ein Test.", encoding="utf-8")
    monkeypatch.setattr(loc_mod, "_langdetect_from_sample", lambda _sample: "de-DE")
    assert loc_mod.resolve_document_locale_bcp47(str(path)) == "de-DE"


ODF_CONTENT_MIXED = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
  <office:automatic-styles>
    <style:style style:name="German" style:family="text">
      <style:text-properties fo:language="de" fo:country="DE"/>
    </style:style>
  </office:automatic-styles>
  <office:body><office:text>
    <text:p>Hello. <text:span text:style-name="German">Guten Tag.</text:span></text:p>
  </office:text></office:body>
</office:document-content>"""

ODF_STYLES_EN = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
  <office:styles>
    <style:default-style style:family="paragraph">
      <style:text-properties fo:language="en" fo:country="US"/>
    </style:default-style>
  </office:styles>
</office:document-styles>"""

DOCX_DOCUMENT_MIXED = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:rPr><w:lang w:val="en-US"/></w:rPr><w:t>Hello. </w:t></w:r>
      <w:r><w:rPr><w:lang w:val="de-DE"/></w:rPr><w:t>Guten Tag.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""


def _write_odt_with_content(path: Path, *, content: bytes, styles: bytes | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", content)
        if styles is not None:
            zf.writestr("styles.xml", styles)


def test_load_odf_style_locale_map_includes_automatic_styles() -> None:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(ODF_CONTENT_MIXED)
    style_map = loc_mod.load_odf_style_locale_map(root)
    assert style_map["German"] == "de-DE"


def test_extract_odf_paragraph_runs_mixed_locales(tmp_path: Path) -> None:
    odt = tmp_path / "mixed.odt"
    _write_odt_with_content(odt, content=ODF_CONTENT_MIXED, styles=ODF_STYLES_EN)
    paragraphs = loc_mod.extract_odf_paragraph_runs(str(odt))
    assert len(paragraphs) == 1
    passage, runs = paragraphs[0]
    assert passage == "Hello. Guten Tag."
    assert len(runs) == 2
    assert runs[0].locale_bcp47 == "en-US"
    assert runs[1].locale_bcp47 == "de-DE"
    assert passage[runs[0].char_start : runs[0].char_end] == "Hello. "
    assert passage[runs[1].char_start : runs[1].char_end] == "Guten Tag."


def test_extract_odf_merges_adjacent_same_locale_runs(tmp_path: Path) -> None:
    content = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text>
    <text:p><text:span>One </text:span><text:span>two.</text:span></text:p>
  </office:text></office:body>
</office:document-content>"""
    odt = tmp_path / "merged.odt"
    _write_odt_with_content(odt, content=content, styles=ODF_STYLES_EN)
    _passage, runs = loc_mod.extract_odf_paragraph_runs(str(odt))[0]
    assert len(runs) == 1


def test_extract_docx_paragraph_runs_mixed_locales(tmp_path: Path) -> None:
    docx = tmp_path / "mixed.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", DOCX_DOCUMENT_MIXED)
        zf.writestr("docProps/core.xml", DOCX_CORE_EN)
    paragraphs = loc_mod.extract_docx_paragraph_runs(str(docx))
    assert len(paragraphs) == 1
    passage, runs = paragraphs[0]
    assert passage == "Hello. Guten Tag."
    assert len(runs) == 2
    assert runs[0].locale_bcp47 == "en-US"
    assert runs[1].locale_bcp47 == "de-DE"


def test_locale_runs_for_plain_passage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loc_mod, "_langdetect_from_sample", lambda _sample: "fr-FR")
    runs = loc_mod.locale_runs_for_plain_passage("Bonjour.", fallback_doc_locale="en-US")
    assert len(runs) == 1
    assert runs[0].locale_bcp47 == "fr-FR"
    assert runs[0].char_start == 0
    assert runs[0].char_end == len("Bonjour.")
