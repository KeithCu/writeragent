# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.address_utils import format_address, parse_address, split_sheet_prefix
from plugin.calc.navigation import (
    WRITERAGENT_CELL_URL_PREFIX,
    _accessible_text,
    _char_index_at_point,
    _click_text_index,
    cell_link_registry,
    cell_ref_at_index,
    lookup_cell_ref_at_index,
    normalize_cell_address,
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


def test_normalize_cell_address_canonical():
    assert normalize_cell_address("'a.b'.C3") == "'a.b'.C3"
    assert normalize_cell_address("'O''Brien'.A1") == "'O''Brien'.A1"
    assert normalize_cell_address("$Sheet1.$A$1") == "Sheet1.A1"
    assert normalize_cell_address("A01") == "A1"
    assert normalize_cell_address("XFD1048576") == "XFD1048576"
    assert normalize_cell_address("A0") is None
    assert normalize_cell_address("A1048577") is None
    assert normalize_cell_address("ZZZ1") is None
    assert normalize_cell_address("Sheet1.A1:B2") is None
    assert normalize_cell_address("not-a-cell") is None
    assert normalize_cell_address(" cell://Sheet1!b2 ") == "Sheet1.B2"
    assert normalize_cell_address(f"{WRITERAGENT_CELL_URL_PREFIX}'O''Brien'.A1") == "'O''Brien'.A1"
    assert normalize_cell_address("B" * 300) is None
    assert normalize_cell_address("Bé1") is None


def test_normalize_cell_address_round_trips():
    cases = [
        ("b2", None),
        ("Sheet1.B2", "Sheet1"),
        ("cell://C10", None),
        (f"{WRITERAGENT_CELL_URL_PREFIX}D4", None),
        ("cell://Sheet1.C3", "Sheet1"),
        ("cell://Sheet1!C3", "Sheet1"),
        ("Orders!A1", "Orders"),
        ("'My Sheet'.B2", "My Sheet"),
        ("'My-Sheet'.B2", "My-Sheet"),
        ("My_Sheet.B2", "My_Sheet"),
        ("'a.b'.C3", "a.b"),
        ("'O''Brien'.A1", "O'Brien"),
        ("$Sheet1.$A$1", "Sheet1"),
        ("A01", None),
        ("XFD1048576", None),
        (" cell://Sheet1!b2 ", "Sheet1"),
        (f"{WRITERAGENT_CELL_URL_PREFIX}'O''Brien'.A1", "O'Brien"),
    ]
    for raw, expected_sheet in cases:
        out = normalize_cell_address(raw)
        assert out is not None
        sheet, a1 = split_sheet_prefix(out)
        col, row = parse_address(a1)
        assert format_address(col, row) == a1
        assert sheet == expected_sheet


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


def test_click_text_index():
    model = SimpleNamespace(SelectionStart=5)
    ctrl = MagicMock()
    ctrl.getModel.return_value = model
    assert _click_text_index(ctrl, 10, 20) == 5


def test_click_text_index_ignores_selection_struct():
    model = SimpleNamespace(Selection=SimpleNamespace(Start=3, End=3))
    ctrl = MagicMock()
    ctrl.getModel.return_value = model
    with patch("plugin.calc.navigation._char_index_at_point", return_value=7):
        assert _click_text_index(ctrl, 10, 20) == 7


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



