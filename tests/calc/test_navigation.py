# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


@pytest.mark.parametrize(
    ("raw", "expected", "expected_sheet"),
    [
        ("b2", "B2", None),
        ("Sheet1.B2", "Sheet1.B2", "Sheet1"),
        ("cell://C10", "C10", None),
        (f"{WRITERAGENT_CELL_URL_PREFIX}D4", "D4", None),
        ("cell://Sheet1.C3", "Sheet1.C3", "Sheet1"),
        # Excel bang in the href is accepted and stored as Calc-style Sheet.C3.
        ("cell://Sheet1!C3", "Sheet1.C3", "Sheet1"),
        ("Orders!A1", "Orders.A1", "Orders"),
        ("'My Sheet'.B2", "My Sheet.B2", "My Sheet"),
        ("'My-Sheet'.B2", "My-Sheet.B2", "My-Sheet"),
        ("My_Sheet.B2", "My_Sheet.B2", "My_Sheet"),
        ("'a.b'.C3", "'a.b'.C3", "a.b"),
        ("'O''Brien'.A1", "'O''Brien'.A1", "O'Brien"),
        ("$Sheet1.$A$1", "Sheet1.A1", "Sheet1"),
        ("A01", "A1", None),
        ("XFD1048576", "XFD1048576", None),
        (" cell://Sheet1!b2 ", "Sheet1.B2", "Sheet1"),
        (f"{WRITERAGENT_CELL_URL_PREFIX}'O''Brien'.A1", "'O''Brien'.A1", "O'Brien"),
        (f"{WRITERAGENT_CELL_URL_PREFIX}My%20Sheet.A1", "My Sheet.A1", "My Sheet"),
    ],
)
def test_normalize_cell_address_valid_and_round_trips(raw, expected, expected_sheet):
    out = normalize_cell_address(raw)
    assert out == expected
    assert out is not None
    sheet, a1 = split_sheet_prefix(out)
    col, row = parse_address(a1)
    assert format_address(col, row) == a1
    assert sheet == expected_sheet


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-cell",
        "A0",
        "A1048577",
        "ZZZ1",
        "Sheet1.A1:B2",
        "B" * 300,
        "Bé1",
    ],
)
def test_normalize_cell_address_invalid(raw):
    assert normalize_cell_address(raw) is None


def test_cell_link_registry_lookup():
    control = MagicMock()
    control.getText.return_value = ""
    cell_link_registry.clear(control)
    try:
        cell_link_registry.add(control, 10, 12, "B2")
        assert lookup_cell_ref_at_index(control, 10) == "B2"
        assert lookup_cell_ref_at_index(control, 11) == "B2"
        assert lookup_cell_ref_at_index(control, 12) is None
    finally:
        cell_link_registry.clear(control)


def test_cell_link_registry_drop_from_keeps_spans_ending_at_cut():
    control = MagicMock()
    control.getText.return_value = ""
    cell_link_registry.clear(control)
    try:
        cell_link_registry.add(control, 0, 5, "A1")
        cell_link_registry.add(control, 3, 8, "B2")
        cell_link_registry.add(control, 8, 10, "C3")
        cell_link_registry.drop_from(control, 5)
        assert lookup_cell_ref_at_index(control, 0) == "A1"
        assert lookup_cell_ref_at_index(control, 4) == "A1"
        assert lookup_cell_ref_at_index(control, 6) is None
        assert lookup_cell_ref_at_index(control, 8) is None
    finally:
        cell_link_registry.clear(control)


def test_cell_link_registry_evicts_spans_on_control_gc():
    import gc
    from plugin.calc.navigation import CellLinkSpanRegistry

    registry = CellLinkSpanRegistry()
    control = MagicMock()
    ctrl_id = id(control)
    registry.add(control, 0, 4, "B2")
    assert ctrl_id in registry._spans

    del control
    gc.collect()
    assert ctrl_id not in registry._spans


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
    )

    ctrl = MagicMock()
    listeners = []
    ctrl.addMouseListener.side_effect = listeners.append

    attach_calc_cell_link_listener(ctrl, lambda: None)
    assert len(listeners) == 1
    listener = listeners[0]

    cell_link_registry.add(ctrl, 0, 5, "A1")
    assert cell_link_registry.lookup(ctrl, 2) == "A1"

    listener.disposing(MagicMock())
    assert cell_link_registry.lookup(ctrl, 2) is None


def test_cell_link_click_navigates_with_doc_and_address():
    try:
        import uno  # noqa: F401
        from com.sun.star.awt import XMouseListener  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("PyUNO XMouseListener unavailable")

    from types import SimpleNamespace

    from plugin.calc.navigation import attach_calc_cell_link_listener, cell_link_registry

    ctrl = MagicMock()
    ctrl.getModel.return_value = SimpleNamespace(SelectionStart=2)
    listeners = []
    ctrl.addMouseListener.side_effect = listeners.append
    doc = MagicMock()

    cell_link_registry.add(ctrl, 0, 5, "Sheet1.B2")
    attach_calc_cell_link_listener(ctrl, lambda: doc)
    listener = listeners[0]
    try:
        with patch("plugin.calc.navigation.navigate_to_cell", return_value=True) as nav:
            listener.mouseReleased(SimpleNamespace(Buttons=1, ClickCount=1, X=0, Y=0))
        nav.assert_called_once_with(doc, "Sheet1.B2")
    finally:
        listener.disposing(MagicMock())



def _font_state(style_name: str) -> tuple[SimpleNamespace, ...]:
    """A ``.uno:CharFontNameCJK`` status State as ORichTextPeer reports it."""
    return (
        SimpleNamespace(Name="CharFontNameCJK.StyleName", Value=style_name),
        SimpleNamespace(Name="CharFontNameCJK.FamilyName", Value="Liberation Sans"),
    )


def test_cell_address_from_font_state_reads_tagged_style_name():
    from plugin.calc.navigation import CELL_LINK_FONT_PREFIX, cell_address_from_font_state

    assert cell_address_from_font_state(_font_state(CELL_LINK_FONT_PREFIX + "Q1 Data.C5")) == "Q1 Data.C5"
    assert cell_address_from_font_state(_font_state(CELL_LINK_FONT_PREFIX + "Sheet2.A1")) == "Sheet2.A1"
    assert cell_address_from_font_state(_font_state("")) is None
    assert cell_address_from_font_state(_font_state("Bold")) is None
    assert cell_address_from_font_state(_font_state(CELL_LINK_FONT_PREFIX + "not a cell")) is None
    assert cell_address_from_font_state(None) is None


def test_mark_cell_link_range_sets_and_clears_tag():
    from plugin.calc.navigation import CELL_LINK_FONT_PREFIX, CELL_LINK_FONT_PROPERTY, mark_cell_link_range

    rng = MagicMock()
    mark_cell_link_range(rng, "Sheet2.B3")
    rng.setPropertyValue.assert_called_once_with(CELL_LINK_FONT_PROPERTY, CELL_LINK_FONT_PREFIX + "Sheet2.B3")
    rng.reset_mock()
    mark_cell_link_range(rng, None)
    rng.setPropertyValue.assert_called_once_with(CELL_LINK_FONT_PROPERTY, "")


def _attached_status_listener(doc):
    """Attach to a mock control with no caret index; return (listener, status_dispatch)."""
    try:
        import uno  # noqa: F401
        from com.sun.star.awt import XMouseListener  # noqa: F401
    except ImportError:
        pytest.skip("PyUNO XMouseListener unavailable")

    from plugin.calc.navigation import attach_calc_cell_link_listener

    ctrl = MagicMock()
    ctrl.getModel.return_value = SimpleNamespace()
    ctrl.getAccessibleContext.return_value = None
    listeners = []
    ctrl.addMouseListener.side_effect = listeners.append
    dispatch = ctrl.getPeer.return_value.queryDispatch.return_value
    attach_calc_cell_link_listener(ctrl, lambda: doc)
    listener = listeners[0]
    dispatch.addStatusListener.assert_called_once()
    assert dispatch.addStatusListener.call_args[0][0] is listener
    assert dispatch.addStatusListener.call_args[0][1].Complete == ".uno:CharFontNameCJK"
    return listener, dispatch


def _click(listener, x=10, y=20, *, release_x=None, release_y=None, modifiers=0):
    listener.mousePressed(SimpleNamespace(Buttons=1, ClickCount=1, X=x, Y=y, Modifiers=modifiers))
    listener.mouseReleased(
        SimpleNamespace(
            Buttons=1,
            ClickCount=1,
            X=x if release_x is None else release_x,
            Y=y if release_y is None else release_y,
            Modifiers=modifiers,
        )
    )


def test_cell_link_click_uses_caret_font_status():
    from plugin.calc.navigation import CELL_LINK_FONT_PREFIX

    doc = MagicMock()
    listener, _dispatch = _attached_status_listener(doc)
    try:
        listener.statusChanged(SimpleNamespace(State=_font_state(CELL_LINK_FONT_PREFIX + "Q1 Data.C5")))
        with patch("plugin.calc.navigation.navigate_to_cell", return_value=True) as nav:
            _click(listener)
        nav.assert_called_once_with(doc, "Q1 Data.C5")
    finally:
        listener.disposing(MagicMock())


def test_cell_link_click_ignores_drag_modifiers_and_plain_text():
    from plugin.calc.navigation import CELL_LINK_FONT_PREFIX

    doc = MagicMock()
    listener, _dispatch = _attached_status_listener(doc)
    try:
        listener.statusChanged(SimpleNamespace(State=_font_state(CELL_LINK_FONT_PREFIX + "Sheet2.B3")))
        with patch("plugin.calc.navigation.navigate_to_cell", return_value=True) as nav:
            _click(listener, release_x=60)  # drag-select across the link
            _click(listener, modifiers=1)  # shift+click extends a selection
            listener.statusChanged(SimpleNamespace(State=_font_state("")))
            _click(listener)  # caret in plain text
        nav.assert_not_called()
    finally:
        listener.disposing(MagicMock())


def test_cell_link_click_waits_for_late_caret_status():
    from plugin.calc.navigation import CELL_LINK_FONT_PREFIX

    doc = MagicMock()
    listener, _dispatch = _attached_status_listener(doc)
    try:
        with patch("plugin.calc.navigation.navigate_to_cell", return_value=True) as nav:
            _click(listener)
            nav.assert_not_called()
            listener.statusChanged(SimpleNamespace(State=_font_state(CELL_LINK_FONT_PREFIX + "A1")))
            # A later caret move (keyboard) is not a click.
            listener.statusChanged(SimpleNamespace(State=_font_state(CELL_LINK_FONT_PREFIX + "B2")))
        nav.assert_called_once_with(doc, "A1")
    finally:
        listener.disposing(MagicMock())


def test_status_dispatch_disposing_keeps_mouse_listener():
    from plugin.calc.navigation import _CELL_LINK_LISTENERS, CELL_LINK_FONT_PREFIX

    listener, dispatch = _attached_status_listener(MagicMock())
    listener.statusChanged(SimpleNamespace(State=_font_state(CELL_LINK_FONT_PREFIX + "A1")))
    listener.disposing(dispatch)
    assert listener.caret_addr is None
    assert listener in _CELL_LINK_LISTENERS.values()
    listener.disposing(MagicMock())
    assert listener not in _CELL_LINK_LISTENERS.values()
