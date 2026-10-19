# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.navigation import (
    WRITERAGENT_CELL_URL_PREFIX,
    _accessible_text,
    _char_index_at_point,
    _click_text_index,
    cell_link_registry,
    cell_ref_at_index,
    extract_cell_links_from_html,
    lookup_cell_ref_at_index,
    normalize_cell_address,
    portion_cell_href,
    portion_looks_like_cell_link,
    render_calc_cell_refs,
)


def test_normalize_cell_address_simple():
    assert normalize_cell_address("b2") == "B2"
    assert normalize_cell_address("Sheet1.B2") == "Sheet1.B2"
    assert normalize_cell_address("cell://C10") == "C10"
    assert normalize_cell_address(f"{WRITERAGENT_CELL_URL_PREFIX}D4") == "D4"
    assert normalize_cell_address("not-a-cell") is None
    assert normalize_cell_address("cell://Sheet1.C3") == "Sheet1.C3"
    # Excel bang in the href is accepted and stored as Calc-style Sheet.C3.
    assert normalize_cell_address("cell://Sheet1!C3") == "Sheet1.C3"
    assert normalize_cell_address("Orders!A1") == "Orders.A1"

    # Advanced sheet names (quotes, spaces, hyphens)
    assert normalize_cell_address("'My Sheet'.B2") == "My Sheet.B2"
    assert normalize_cell_address("'My-Sheet'.B2") == "My-Sheet.B2"
    assert normalize_cell_address("My_Sheet.B2") == "My_Sheet.B2"


def test_render_calc_cell_refs_html():
    text = (
        '<p>See <a href="cell://B2">B2</a> and <a href=\'cell://Sheet1.C3\'>C3</a> '
        'and <a href="cell://Orders!A1">A1</a>.</p>'
    )
    out = render_calc_cell_refs(text)
    assert f'href="{WRITERAGENT_CELL_URL_PREFIX}B2"' in out
    assert f"href='{WRITERAGENT_CELL_URL_PREFIX}Sheet1.C3'" in out
    assert f'href="{WRITERAGENT_CELL_URL_PREFIX}Orders.A1"' in out
    assert 'href="cell://' not in out.lower()
    assert "href='cell://" not in out.lower()


def test_extract_cell_links_from_html():
    html = (
        '<p>See <a href="cell://B2">B2</a> and <a href="writeragent-cell://C3">cell C3</a> '
        'and <a href="cell://Orders!A1">jump</a>.</p>'
    )
    assert extract_cell_links_from_html(html) == [
        ("B2", "B2"),
        ("cell C3", "C3"),
        ("jump", "Orders.A1"),
    ]


def test_render_calc_cell_refs_passthrough():
    assert render_calc_cell_refs("no links here") == "no links here"
    assert render_calc_cell_refs("") == ""


def test_cell_link_registry_lookup():
    control = MagicMock()
    cell_link_registry.clear(control)
    cell_link_registry.add(control, 10, 12, "B2")
    assert lookup_cell_ref_at_index(control, 10) == "B2"
    assert lookup_cell_ref_at_index(control, 11) == "B2"
    assert lookup_cell_ref_at_index(control, 12) is None


def test_cell_link_registry_drop_from_keeps_spans_ending_at_cut():
    control = MagicMock()
    cell_link_registry.clear(control)
    cell_link_registry.add(control, 0, 5, "A1")
    cell_link_registry.add(control, 3, 8, "B2")
    cell_link_registry.add(control, 8, 10, "C3")
    cell_link_registry.drop_from(control, 5)
    assert lookup_cell_ref_at_index(control, 0) == "A1"
    assert lookup_cell_ref_at_index(control, 4) == "A1"
    assert lookup_cell_ref_at_index(control, 6) is None
    assert lookup_cell_ref_at_index(control, 8) is None


def test_cell_ref_at_index_html():
    text = '<p>See <a href="writeragent-cell://B2">B2</a> here.</p>'
    idx = text.index("writeragent-cell")
    assert cell_ref_at_index(text, idx) == "B2"
    assert cell_ref_at_index(text, 0) is None


def test_accessible_text_query_interface_uses_uno_type():
    """Imported IDL classes break queryInterface under pyuno; use getTypeByName."""
    control = MagicMock()
    ctx = MagicMock()
    axtext = MagicMock()
    control.getAccessibleContext.return_value = ctx
    type_mock = MagicMock()
    with patch("plugin.calc.calc_utils.uno") as mock_uno:
        mock_uno.getTypeByName.return_value = type_mock
        ctx.queryInterface.return_value = axtext
        assert _accessible_text(control) is axtext
        mock_uno.getTypeByName.assert_called_with("com.sun.star.accessibility.XAccessibleText")
        ctx.queryInterface.assert_called_once_with(type_mock)


def test_portion_cell_href():
    p = SimpleNamespace(HyperLinkURL="cell://Sheet1.A1")
    assert portion_cell_href(p) == "cell://Sheet1.A1"
    assert portion_cell_href(None) is None
    p_none = SimpleNamespace()
    assert portion_cell_href(p_none) is None


def test_portion_looks_like_cell_link():
    p = SimpleNamespace(CharUnderline=1)
    assert portion_looks_like_cell_link(p, "A1") is True
    assert portion_looks_like_cell_link(p, "not_cell") is False
    p_no_ul = SimpleNamespace(CharUnderline=0)
    assert portion_looks_like_cell_link(p_no_ul, "A1") is False


def test_click_text_index():
    model = SimpleNamespace(SelectionStart=5)
    ctrl = MagicMock()
    ctrl.getModel.return_value = model
    assert _click_text_index(ctrl, 10, 20) == 5


def test_accessible_text_returns_none_on_exception():
    control = MagicMock()
    control.getAccessibleContext.side_effect = RuntimeError("accessibility error")
    assert _accessible_text(control) is None


def test_char_index_at_point_returns_none_on_exception():
    control = MagicMock()
    ctx = MagicMock()
    axtext = MagicMock()
    control.getAccessibleContext.return_value = ctx
    type_mock = MagicMock()
    with patch("plugin.calc.calc_utils.uno") as mock_uno:
        mock_uno.getTypeByName.return_value = type_mock
        ctx.queryInterface.return_value = axtext
        axtext.getIndexAtPoint.side_effect = RuntimeError("point error")
        assert _char_index_at_point(control, 10, 20) is None


def test_attach_calc_cell_link_listener_disposing_clears_control_spans():
    try:
        import uno  # noqa: F401
        from com.sun.star.awt import XMouseListener  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("PyUNO XMouseListener unavailable")

    from plugin.calc.navigation import (
        attach_calc_cell_link_listener,
        cell_link_registry,
        register_cell_link_span,
    )

    ctrl = MagicMock()
    listeners = []
    ctrl.addMouseListener.side_effect = listeners.append

    ctx = MagicMock()
    attach_calc_cell_link_listener(ctx, ctrl, lambda: None)
    assert len(listeners) == 1
    listener = listeners[0]

    register_cell_link_span(ctrl, 0, 5, "A1")
    assert cell_link_registry.lookup(ctrl, 2) == "A1"

    listener.disposing(MagicMock())
    assert cell_link_registry.lookup(ctrl, 2) is None



