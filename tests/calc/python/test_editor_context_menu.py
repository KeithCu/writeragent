# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell context menu detection helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.editor_context_menu import _looks_like_cell_context_menu
from plugin.framework.constants import EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
import pytest


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(".uno:Cut", True, id="test_looks_like_cell_context_menu_matches_cut"),
        pytest.param(".uno:Insert", False, id="test_looks_like_cell_context_menu_rejects_other_menus"),
    ],
)
def test_looks_like_cell_context_menu_matches_cut(value, expected):
    first = MagicMock()
    first.getPropertyValue.return_value = value
    container = MagicMock()
    container.getCount.return_value = 1
    container.getByIndex.return_value = first
    assert _looks_like_cell_context_menu(container) is expected

def test_register_frame_uses_uno_type_by_name():
    from plugin.calc.python.editor_context_menu import _register_frame

    frame = MagicMock()
    controller = MagicMock()
    frame.getController.return_value = controller

    with patch("uno.getTypeByName") as mock_get_type:
        mock_type = MagicMock()
        mock_get_type.return_value = mock_type

        _register_frame(frame)

        mock_get_type.assert_any_call("com.sun.star.ui.XContextMenuInterception")
        controller.queryInterface.assert_called_with(mock_type)


def test_edit_python_cell_url_follows_librepy_extension_id():
    from plugin.calc.python.editor_context_menu import _edit_python_cell_url
    from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    try:
        assert _edit_python_cell_url() == f"{EXTENSION_ID_LIBREPY}:scripting.edit_python_cell"
    finally:
        reset_package_extension_id_for_tests()


def test_edit_python_cell_url_follows_writeragent_extension_id():
    from plugin.calc.python.editor_context_menu import _edit_python_cell_url
    from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_WRITERAGENT)
    try:
        assert _edit_python_cell_url() == f"{EXTENSION_ID_WRITERAGENT}:scripting.edit_python_cell"
    finally:
        reset_package_extension_id_for_tests()


def test_install_attaches_global_document_event_listener():
    from plugin.calc.python import editor_context_menu as mod

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        mod.install_calc_cell_context_menu(ctx)
        mod.install_calc_cell_context_menu(ctx)
    smgr.createInstanceWithContext.assert_called_with("com.sun.star.frame.GlobalEventBroadcaster", ctx)
    assert broadcaster.addDocumentEventListener.call_count == 1

