# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision HTML export helpers."""

from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock, patch

import pytest

from plugin.vision.vision_common import CSS_INLINE_INSTALL_CMD
from plugin.vision.venv.vision_html_export import (
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
    with patch("css_inline.CSSInliner") as mock_cls:
        mock_inliner = MagicMock()
        mock_inliner.inline.return_value = '<h1 style="color: blue;">Hi</h1>'
        mock_cls.return_value = mock_inliner
        out = prepare_html_for_lo_import(raw)
    mock_cls.assert_called_once_with(load_remote_stylesheets=False)
    mock_inliner.inline.assert_called_once_with(raw)
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
    raw = '<p>Body line</p><p class="x">Also bare</p>'
    out = augment_lo_body_paragraph_styles(raw)
    assert "font-family: Arial, sans-serif" in out
    assert out.count("font-family: Arial") == 2


def test_augment_lo_body_paragraph_styles_skips_existing_style():
    raw = '<p style="color: red;">Styled</p><p>Plain</p>'
    out = augment_lo_body_paragraph_styles(raw)
    assert out.count("font-family: Arial") == 1
    assert 'style="color: red;"' in out


def test_prepare_html_for_lo_import_applies_heading_and_body_augment():
    raw = "<html><head><style>h2 { color: blue; } p { margin: 1em; }</style></head><body><h2>Hi</h2><p>there</p></body></html>"
    with patch("css_inline.CSSInliner") as mock_cls:
        mock_inliner = MagicMock()
        mock_inliner.inline.return_value = '<h2 style="color: blue;">Hi</h2><p>there</p>'
        mock_cls.return_value = mock_inliner
        out = prepare_html_for_lo_import(raw)
    mock_cls.assert_called_once_with(load_remote_stylesheets=False)
    mock_inliner.inline.assert_called_once_with(raw)
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


def test_html_table_from_columns_rows_clamps_huge_spans():
    from plugin.vision.venv.vision_html_export import _html_table_from_columns_rows

    html = _html_table_from_columns_rows(
        ["A", "B"],
        [["1", "2"], ["3", "4"]],
        [
            {"row": 0, "col": 0, "rowspan": 99999, "colspan": 99999},
            {"row": "junk", "col": 0, "rowspan": 1, "colspan": 1},
            {"row": -1, "col": 0, "rowspan": 2, "colspan": 2},
        ],
    )
    # 3 total rows (1 header + 2 body) and 2 cols: rowspan clamped to 3, colspan to 2
    assert 'rowspan="3"' in html
    assert 'colspan="2"' in html
    assert "99999" not in html
    assert "<table" in html


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


def test_export_docling_to_html_logs_warning_when_no_export_method(caplog):
    obj = object()
    with caplog.at_level(logging.WARNING):
        out = export_docling_to_html(obj, {})
    assert out == ""
    assert "lacks export_to_html" in caplog.text


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
    assert out["html_docling"] == "<p>docling export</p>"
    assert result["html"] == "<p>docling export</p>"  # input dict not mutated


def test_apply_structured_insert_html_skips_html_mode():
    result = {"status": "ok", "helper": "extract_text", "html": "<p>x</p>", "regions": []}
    out = apply_structured_insert_html(result, {"insert_mode": "html"})
    assert out is result


def test_html_from_paddle_structure_skips_escaped_table_paragraph():
    """Raw PP-Structure ``<table>`` block text must not sit beside the real table."""
    raw_table = "<table><tr><td>Widget</td><td>2</td></tr></table>"
    with patch(
        "plugin.vision.venv.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [
                {"type": "text", "text": "Invoice"},
                {"type": "table", "text": raw_table, "box": [0, 0, 10, 10]},
            ],
            [{"columns": ["Item", "Qty"], "rows": [["Widget", "2"]]}],
        )
    assert "Invoice" in html
    assert html.lower().count("<table") == 1
    assert "&lt;table" not in html.lower()
    assert "<td>Widget</td>" in html


def test_html_from_paddle_structure_keeps_plain_table_text():
    with patch(
        "plugin.vision.venv.vision_html_export.prepare_html_for_lo_import",
        side_effect=lambda html: html,
    ):
        html = html_from_paddle_structure(
            [{"type": "table", "text": "See totals"}],
            [{"columns": ["A"], "rows": [["1"]]}],
        )
    assert "See totals" in html
    assert html.lower().count("<table") == 1
    assert "&lt;table" not in html.lower()


def test_extension_root_is_three_parents_above_venv_dir(tmp_path, monkeypatch):
    from plugin.vision.venv import vision_html_export as html_export

    fake = tmp_path / "ext" / "plugin" / "vision" / "venv" / "vision_html_export.py"
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(html_export, "__file__", str(fake))
    assert html_export._extension_root() == str(tmp_path / "ext")


def test_convert_latex_uses_vendored_latex2mathml_when_not_installed(tmp_path, monkeypatch):
    """Vendored copy under the extension root must convert TeX when import fails."""
    import builtins
    import sys

    from plugin.vision.venv import vision_html_export as html_export

    ext = tmp_path / "ext"
    vendor_pkg = ext / "vendor" / "latex2mathml"
    vendor_pkg.mkdir(parents=True)
    (vendor_pkg / "__init__.py").write_text("")
    (vendor_pkg / "converter.py").write_text(
        "def convert(latex, display='inline'):\n"
        "    return '<math display=\"%s\"><mi>x</mi></math>' % display\n"
    )
    parent_pkg = tmp_path / "vendor" / "latex2mathml"
    parent_pkg.mkdir(parents=True)
    (parent_pkg / "__init__.py").write_text("")
    (parent_pkg / "converter.py").write_text(
        "def convert(latex, display='inline'):\n"
        "    raise AssertionError('parent-of-repo latex2mathml must not be used')\n"
    )
    fake = ext / "plugin" / "vision" / "venv" / "vision_html_export.py"
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(html_export, "__file__", str(fake))

    removed = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "latex2mathml" or name.startswith("latex2mathml.")
    }
    vendor = str(ext / "vendor")
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if (name == "latex2mathml" or str(name).startswith("latex2mathml.")) and vendor not in sys.path:
            raise ImportError("latex2mathml is not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    path_before = list(sys.path)
    html_export._import_latex2mathml_convert.cache_clear()
    try:
        out = convert_latex_delimiters_to_mathml("<p>$$x^2$$</p>")
    finally:
        sys.path[:] = path_before
        for name in list(sys.modules):
            if name == "latex2mathml" or name.startswith("latex2mathml."):
                mod_file = getattr(sys.modules[name], "__file__", "") or ""
                if str(tmp_path) in mod_file:
                    sys.modules.pop(name, None)
        sys.modules.update(removed)
        html_export._import_latex2mathml_convert.cache_clear()

    assert "$$" not in out
    assert '<math display="block">' in out


def test_convert_latex_delimiters_to_mathml_display_and_inline():
    pytest.importorskip("latex2mathml")
    html = "<p>$$E=mc^2$$</p><p>see \\(a+b\\) in text</p>"
    out = convert_latex_delimiters_to_mathml(html)
    assert "$$" not in out
    assert "\\(" not in out
    assert "<math" in out
    assert 'display="block"' in out
    assert 'display="inline"' in out


def test_convert_latex_delimiters_unescapes_entities():
    pytest.importorskip("latex2mathml")
    html = "<p>$a &lt; b^2$</p>"
    out = convert_latex_delimiters_to_mathml(html)
    assert "<math" in out
    assert "&lt;" not in out
    assert "<mi>&</mi>" not in out


def test_convert_latex_delimiters_does_not_pair_across_tags():
    pytest.importorskip("latex2mathml")
    html = "<p>costs $alpha</p><p>and $x^2</p>"
    out = convert_latex_delimiters_to_mathml(html)
    # The '$' must not cross </p><p>
    assert "</p><p>" in out
    assert "<p>costs $alpha</p>" in out


def test_convert_latex_delimiters_skips_code_and_pre():
    pytest.importorskip("latex2mathml")
    html = "<p><code>$x^2$</code> and <pre>$y^2$</pre></p>"
    out = convert_latex_delimiters_to_mathml(html)
    assert "<math" not in out
    assert "<code>$x^2$</code>" in out
    assert "<pre>$y^2$</pre>" in out


def test_convert_latex_delimiters_failed_double_dollar_emits_both_dollars():
    # If $$ conversion fails, both $$ must be emitted without pairing subsequent single $
    with patch("plugin.vision.venv.vision_html_export._latex_to_math_element", return_value=None):
        html = "<p>$$unconvertible$$ then $foo then $bar</p>"
        out = convert_latex_delimiters_to_mathml(html)
    assert "$$unconvertible$$" in out
    # Second $ of $$ must not have consumed $foo as inline math
    assert "$foo" in out


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
        '<tr><th colspan="3">ASSETS:</th></tr>'
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


def test_promote_table_header_rows_caption_and_colgroup():
    raw = (
        "<table>"
        "<caption>Inventory Summary</caption>"
        '<colgroup><col style="width:50%"><col style="width:50%"></colgroup>'
        "<tbody>"
        "<tr><th>Item</th><th>Qty</th></tr>"
        "<tr><td>Pen</td><td>5</td></tr>"
        "</tbody></table>"
    )
    out = promote_table_header_rows(raw)
    assert "<caption>Inventory Summary</caption>" in out
    assert "<colgroup>" in out
    # Caption and colgroup must precede thead
    caption_pos = out.index("<caption>")
    colgroup_pos = out.index("<colgroup>")
    thead_pos = out.index("<thead>")
    tbody_pos = out.index("<tbody>")
    assert caption_pos < colgroup_pos < thead_pos < tbody_pos
    assert "<thead><tr><th>Item</th><th>Qty</th></tr></thead>" in out
    assert "<tbody><tr><td>Pen</td><td>5</td></tr></tbody>" in out


def test_promote_table_header_rows_skips_complex_tbody_or_tfoot():
    # When tfoot or multiple tbodys are present, table must not be rewritten into broken markup
    raw = (
        "<table><tbody>"
        "<tr><th>Item</th></tr>"
        "<tr><td>Pen</td></tr>"
        "</tbody><tfoot><tr><td>Total</td></tr></tfoot></table>"
    )
    out = promote_table_header_rows(raw)
    assert out == raw


def test_promote_table_header_rows_skips_layout_tables():
    raw = '<table style="width:100%;border:none;"><tr><td>Left</td><td>Right</td></tr></table>'
    out = promote_table_header_rows(raw)
    assert "<thead>" not in out


def test_prepare_html_for_lo_import_applies_table_and_math_augment():
    raw = "<html><body><p>$$x^2$$</p><table><tr><th>A</th></tr><tr><td>1</td></tr></table></body></html>"
    with patch("css_inline.CSSInliner") as mock_cls:
        mock_inliner = MagicMock()
        mock_inliner.inline.return_value = "<p>$$x^2$$</p><table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
        mock_cls.return_value = mock_inliner
        out = prepare_html_for_lo_import(raw)
    mock_cls.assert_called_once_with(load_remote_stylesheets=False)
    assert "<math" in out
    assert 'border="1"' in out
    assert "<thead>" in out
    assert "background-color: #f0f0f0" in out
