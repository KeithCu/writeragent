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
