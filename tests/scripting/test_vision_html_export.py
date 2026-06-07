# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision HTML export helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.vision_html_export import (
    CSS_INLINE_INSTALL_CMD,
    augment_lo_body_paragraph_styles,
    augment_lo_heading_styles,
    export_docling_to_html,
    html_from_paddle_regions,
    html_from_paddle_structure,
    prepare_html_for_lo_import,
)


def test_prepare_html_for_lo_import_inlines():
    raw = "<html><head><style>h1 { color: blue; }</style></head><body><h1>Hi</h1></body></html>"
    with patch("css_inline.inline", return_value='<h1 style="color: blue;">Hi</h1>') as mock_inline:
        out = prepare_html_for_lo_import(raw)
    mock_inline.assert_called_once_with(raw)
    assert "style=" in out


def test_prepare_html_for_lo_import_empty_passthrough():
    assert prepare_html_for_lo_import("") == ""
    assert prepare_html_for_lo_import("   ") == "   "


def test_augment_lo_heading_styles_merges_into_existing_style():
    raw = '<h2 style="color: #333;">Title</h2>'
    out = augment_lo_heading_styles(raw)
    assert "font-weight: bold" in out
    assert "font-size: 14pt" in out
    assert "color: #333" in out


def test_augment_lo_heading_styles_adds_style_when_missing():
    raw = "<h2>Title</h2>"
    out = augment_lo_heading_styles(raw)
    assert 'style="font-size: 14pt; font-weight: bold;"' in out


def test_augment_lo_body_paragraph_styles_bare_p():
    raw = "<p>Body line</p><p class=\"x\">Also bare</p>"
    out = augment_lo_body_paragraph_styles(raw)
    assert 'font-family: Arial, sans-serif' in out
    assert out.count("font-family: Arial") == 2


def test_augment_lo_body_paragraph_styles_skips_existing_style():
    raw = '<p style="color: red;">Styled</p><p>Plain</p>'
    out = augment_lo_body_paragraph_styles(raw)
    assert out.count("font-family: Arial") == 1
    assert 'style="color: red;"' in out


def test_prepare_html_for_lo_import_applies_heading_and_body_augment():
    raw = "<html><head><style>h2 { color: blue; } p { margin: 1em; }</style></head><body><h2>Hi</h2><p>there</p></body></html>"
    with patch("css_inline.inline", return_value='<h2 style="color: blue;">Hi</h2><p>there</p>') as mock_inline:
        out = prepare_html_for_lo_import(raw)
    mock_inline.assert_called_once_with(raw)
    assert "font-weight: bold" in out
    assert "font-family: Arial" in out


def test_html_from_paddle_regions_escapes_and_wraps():
    with patch(
        "plugin.scripting.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_regions([{"text": "Line & one"}, {"text": "Line two"}])
    assert "<p>Line &amp; one</p>" in html
    assert "<p>Line two</p>" in html
    assert "Arial" in html


def test_html_from_paddle_structure_table_and_heading():
    with patch(
        "plugin.scripting.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [{"type": "section_header", "text": "Title", "box": [0, 0, 0, 0]}],
            [{"columns": ["A", "B"], "rows": [["1", "2"]]}],
        )
    assert "<h2>Title</h2>" in html
    assert "<table" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_export_docling_to_html_default():
    doc = MagicMock()
    doc.export_to_html.return_value = "<p><strong>Hi</strong></p>"
    fake = MagicMock()
    fake.ImageRefMode.EMBEDDED = "embedded"
    with patch("plugin.scripting.vision_html_export.importlib.import_module", return_value=fake), patch(
        "plugin.scripting.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        out = export_docling_to_html(doc, {})
    assert "strong" in out
    doc.export_to_html.assert_called_once_with(image_mode=fake.ImageRefMode.EMBEDDED, split_page_view=False)


def test_css_inline_install_cmd():
    assert "css-inline" in CSS_INLINE_INSTALL_CMD
