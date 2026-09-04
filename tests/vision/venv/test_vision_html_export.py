# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision HTML export helpers."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from plugin.vision.venv.vision_html_export import (
    CSS_INLINE_INSTALL_CMD,
    apply_structured_insert_html,
    augment_lo_body_paragraph_styles,
    augment_lo_heading_styles,
    augment_lo_table_styles,
    convert_latex_delimiters_to_mathml,
    export_docling_to_html,
    html_from_paddle_regions,
    html_from_paddle_structure,
    prepare_html_for_lo_import,
    promote_table_header_rows,
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
        "plugin.vision.venv.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_regions([{"text": "Line & one"}, {"text": "Line two"}])
    assert "<p>Line &amp; one</p>" in html
    assert "<p>Line two</p>" in html
    assert "Arial" in html


def test_html_from_paddle_structure_table_and_heading():
    with patch(
        "plugin.vision.venv.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [{"type": "section_header", "text": "Title", "box": [0, 0, 0, 0]}],
            [{"columns": ["A", "B"], "rows": [["1", "2"]]}],
        )
    assert "<h2>Title</h2>" in html
    assert "<table" in html
    assert "<thead>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html
    assert 'border="1"' in html


def test_html_table_from_columns_rows_emits_spans():
    from plugin.vision.venv.vision_html_export import _html_table_from_columns_rows

    html = _html_table_from_columns_rows(
        ["", "2025", "2024"],
        [["ASSETS:", "", ""], ["Cash", "1", "2"]],
        [{"row": 1, "col": 0, "rowspan": 1, "colspan": 3}],
    )
    assert 'colspan="3"' in html
    assert html.count("ASSETS:") == 1
    assert len(re.findall(r"<t[dh]\b", html)) == 7  # 3 header + 1 spanned + 3 cash row
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "ASSETS:" in html[html.index("<tbody>") :]
    assert "ASSETS:" not in html[html.index("<thead>") : html.index("</thead>")]



def test_export_docling_to_html_default():
    doc = MagicMock()
    doc.export_to_html.return_value = "<p><strong>Hi</strong></p>"
    fake = MagicMock()
    fake.ImageRefMode.PLACEHOLDER = "placeholder"
    with patch("plugin.vision.venv.vision_html_export.importlib.import_module", return_value=fake), patch(
        "plugin.vision.venv.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        out = export_docling_to_html(doc, {})
    assert "strong" in out
    doc.export_to_html.assert_called_once_with(
        image_mode=fake.ImageRefMode.PLACEHOLDER,
        formula_to_mathml=True,
        split_page_view=False,
    )


def test_css_inline_install_cmd():
    assert "css-inline" in CSS_INLINE_INSTALL_CMD


def test_apply_structured_insert_html_replaces_html_in_worker():
    result = {
        "status": "ok",
        "helper": "extract_structure",
        "html": "<p>docling export</p>",
        "blocks": [
            {"type": "text", "text": "Left", "box": [10, 100, 180, 20]},
            {"type": "text", "text": "Right", "box": [420, 102, 180, 20]},
        ],
    }
    with patch(
        "plugin.vision.venv.vision_html_export.structured_html_from_vision_result",
        return_value="<table><tr><td>Left</td><td>Right</td></tr></table>",
    ):
        out = apply_structured_insert_html(result, {"insert_mode": "structured"})
    assert out["html"].startswith("<table>")
    assert out["html"] != result["html"]


def test_apply_structured_insert_html_skips_html_mode():
    result = {"status": "ok", "helper": "extract_text", "html": "<p>x</p>", "regions": []}
    out = apply_structured_insert_html(result, {"insert_mode": "html"})
    assert out is result


def test_convert_latex_delimiters_to_mathml_display_and_inline():
    pytest.importorskip("latex2mathml")
    html = "<p>$$E=mc^2$$</p><p>see \\(a+b\\) in text</p>"
    out = convert_latex_delimiters_to_mathml(html)
    assert "$$" not in out
    assert "\\(" not in out
    assert "<math" in out
    assert 'display="block"' in out
    assert 'display="inline"' in out


def test_convert_latex_delimiters_skips_currency_dollars():
    html = "<td>$ 35,934</td><td>$9.00</td>"
    out = convert_latex_delimiters_to_mathml(html)
    assert "$ 35,934" in out
    assert "$9.00" in out
    assert "<math" not in out


def test_convert_latex_delimiters_leaves_existing_mathml():
    html = '<p><math display="block"><mi>x</mi></math></p>'
    assert convert_latex_delimiters_to_mathml(html) == html


def test_augment_lo_table_styles_adds_border_and_header_chrome():
    raw = "<table><tr><th>Year</th></tr><tr><td>1</td></tr></table>"
    out = augment_lo_table_styles(raw)
    assert 'border="1"' in out
    assert "border-collapse: collapse" in out
    assert "1px solid #ccc" in out
    assert "background-color: #f0f0f0" in out
    assert "font-weight: bold" in out


def test_augment_lo_table_styles_skips_layout_tables():
    raw = (
        '<table style="width:100%;border:none;border-collapse:collapse;">'
        '<tr><td style="border:none;">Left</td>'
        '<td style="border:none;">Right</td></tr></table>'
    )
    out = augment_lo_table_styles(raw)
    assert 'border="1"' not in out
    assert "background-color: #f0f0f0" not in out
    assert "border:none" in out


def test_promote_table_header_rows_wraps_first_th_row_only():
    raw = (
        "<table><tbody>"
        "<tr><td></td><th>2025</th><th>2024</th></tr>"
        "<tr><th colspan=\"3\">ASSETS:</th></tr>"
        "<tr><th>Cash</th><td>1</td><td>2</td></tr>"
        "</tbody></table>"
    )
    out = promote_table_header_rows(raw)
    assert "<thead>" in out
    header = out[out.index("<thead>") : out.index("</thead>")]
    body = out[out.index("<tbody>") :]
    assert "2025" in header
    assert "ASSETS:" not in header
    assert "ASSETS:" in body
    assert len(re.findall(r"<th\b", header)) == 3  # empty corner td promoted to th


def test_promote_table_header_rows_skips_layout_tables():
    raw = (
        '<table style="width:100%;border:none;">'
        "<tr><td>Left</td><td>Right</td></tr></table>"
    )
    out = promote_table_header_rows(raw)
    assert "<thead>" not in out


def test_prepare_html_for_lo_import_applies_table_and_math_augment():
    raw = "<html><body><p>$$x^2$$</p><table><tr><th>A</th></tr><tr><td>1</td></tr></table></body></html>"
    with patch(
        "css_inline.inline",
        return_value="<p>$$x^2$$</p><table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
    ):
        out = prepare_html_for_lo_import(raw)
    assert "<math" in out
    assert 'border="1"' in out
    assert "<thead>" in out
    assert "background-color: #f0f0f0" in out
