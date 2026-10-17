# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell context menu detection helpers."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python import editor_context_menu as mod
from plugin.calc.python.editor_context_menu import (
    _edit_python_cell_url,
    _looks_like_cell_context_menu,
    _register_frame,
)
from plugin.framework.constants import EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id


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


def test_looks_like_cell_context_menu_separator_is_not_a_cell_menu():
    first = MagicMock()
    first.getPropertyValue.side_effect = Exception("Separator has no CommandURL")
    container = MagicMock()
    container.getCount.return_value = 1
    container.getByIndex.return_value = first
    assert _looks_like_cell_context_menu(container) is False
    assert _looks_like_cell_context_menu(None) is False
    empty = MagicMock()
    empty.getCount.return_value = 0
    assert _looks_like_cell_context_menu(empty) is False


def test_notify_inserts_entry_when_source_window_has_no_controller():
    ui_mod = types.ModuleType("com.sun.star.ui")
    ui_mod.__path__ = []

    class _StubXContextMenuInterceptor:
        pass

    class _StubSeparatorType:
        LINE = 0

    ui_mod.XContextMenuInterceptor = _StubXContextMenuInterceptor
    ui_mod.ActionTriggerSeparatorType = _StubSeparatorType

    action_mod = types.ModuleType("com.sun.star.ui.ContextMenuInterceptorAction")
    IGNORED_SENTINEL = object()
    CONTINUE_MODIFIED_SENTINEL = object()
    action_mod.IGNORED = IGNORED_SENTINEL
    action_mod.CONTINUE_MODIFIED = CONTINUE_MODIFIED_SENTINEL

    saved_ui = sys.modules.get("com.sun.star.ui")
    saved_action = sys.modules.get("com.sun.star.ui.ContextMenuInterceptorAction")
    saved_interceptor = mod._interceptor

    sys.modules["com.sun.star.ui"] = ui_mod
    sys.modules["com.sun.star.ui.ContextMenuInterceptorAction"] = action_mod
    mod._interceptor = None

    try:
        interceptor = mod._get_interceptor()

        cut_item = MagicMock()
        cut_item.getPropertyValue.return_value = ".uno:Cut"

        created_instances: list[MagicMock] = []

        def _create_instance(service_name: str) -> MagicMock:
            instance = MagicMock(name=service_name)
            created_instances.append(instance)
            return instance

        factory = MagicMock()
        factory.createInstance.side_effect = _create_instance

        container = MagicMock()
        container.getCount.return_value = 2
        container.getByIndex.side_effect = lambda idx: cut_item if idx == 0 else MagicMock()
        container.queryInterface.return_value = factory

        class PlainSourceWindow:
            pass

        class Event:
            def __init__(self, source_window: Any, trig_container: Any) -> None:
                self.SourceWindow = source_window
                self.ActionTriggerContainer = trig_container

        event = Event(PlainSourceWindow(), container)

        result = interceptor.notifyContextMenuExecute(event)

        assert result is CONTINUE_MODIFIED_SENTINEL
        assert container.insertByIndex.call_count == 2
        first_call = container.insertByIndex.call_args_list[0]
        second_call = container.insertByIndex.call_args_list[1]
        assert first_call[0][0] == 2
        assert second_call[0][0] == 3

        entry_mock = created_instances[1]
        cmd_call = [c for c in entry_mock.setPropertyValue.call_args_list if c[0][0] == "CommandURL"]
        assert cmd_call, "CommandURL property was not set on entry"
        assert str(cmd_call[0][0][1]).endswith(":scripting.edit_python_cell")
    finally:
        mod._interceptor = saved_interceptor
        if saved_ui is not None:
            sys.modules["com.sun.star.ui"] = saved_ui
        else:
            sys.modules.pop("com.sun.star.ui", None)
        if saved_action is not None:
            sys.modules["com.sun.star.ui.ContextMenuInterceptorAction"] = saved_action
        else:
            sys.modules.pop("com.sun.star.ui.ContextMenuInterceptorAction", None)


def test_register_controller_dedups_uno_identity():
    from plugin.calc.python import editor_context_menu as mod

    mod._registered_controllers.clear()

    class StubModel:
        def __init__(self, disposed: bool = False) -> None:
            self.disposed = disposed

        def supportsService(self, service: str) -> bool:
            return service == "com.sun.star.sheet.SpreadsheetDocument"

    class StubController:
        def __init__(self, ident: str) -> None:
            self.ident = ident
            self.model = StubModel()
            self.registered_count = 0

        def __eq__(self, other: Any) -> bool:
            if isinstance(other, StubController):
                return self.ident == other.ident
            return False

        def getModel(self) -> Any:
            if self.model.disposed:
                raise Exception("Controller disposed")
            return self.model

        def queryInterface(self, uno_type: Any) -> Any:
            return self

        def registerContextMenuInterceptor(self, interceptor: Any) -> None:
            self.registered_count += 1

    with patch.object(mod, "_get_interceptor", return_value=MagicMock()):
        c1 = StubController("ident_1")
        c2 = StubController("ident_1")
        c3 = StubController("ident_2")

        mod._register_controller(c1)
        mod._register_controller(c2)
        mod._register_controller(c3)

        assert c1.registered_count == 1
        assert c2.registered_count == 0
        assert c3.registered_count == 1
        assert len(mod._registered_controllers) == 2

        # Mark first disposed and register fourth identity
        c1.model.disposed = True
        c4 = StubController("ident_3")
        mod._register_controller(c4)

        assert c4.registered_count == 1
        assert len(mod._registered_controllers) == 2
        assert c1 not in mod._registered_controllers
        assert c3 in mod._registered_controllers
        assert c4 in mod._registered_controllers


def test_register_frame_uses_uno_type_by_name():
    mod._registered_controllers.clear()

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
    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    try:
        assert _edit_python_cell_url() == f"{EXTENSION_ID_LIBREPY}:scripting.edit_python_cell"
    finally:
        reset_package_extension_id_for_tests()


def test_edit_python_cell_url_follows_writeragent_extension_id():
    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_WRITERAGENT)
    try:
        assert _edit_python_cell_url() == f"{EXTENSION_ID_WRITERAGENT}:scripting.edit_python_cell"
    finally:
        reset_package_extension_id_for_tests()


def test_install_attaches_global_document_event_listener():
    mod._registered_controllers.clear()
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


def test_new_doc_listener_on_document_event():
    mod._registered_controllers.clear()
    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        mod.install_calc_cell_context_menu(ctx)
    listener = mod._doc_listener
    assert listener is not None

    with patch.object(mod, "_register_controller") as mock_reg:
        # Irrelevant event is ignored
        evt = MagicMock()
        evt.EventName = "OnSave"
        listener.on_document_event(evt)
        mock_reg.assert_not_called()

        # Event with ViewController registers it
        evt = MagicMock()
        evt.EventName = "OnViewCreated"
        evt.ViewController = MagicMock()
        listener.on_document_event(evt)
        mock_reg.assert_called_once_with(evt.ViewController)

        # Event without ViewController registers Source.getCurrentController()
        mock_reg.reset_mock()
        evt2 = MagicMock(spec=["EventName", "Source"])
        evt2.EventName = "OnLoadFinished"
        evt2.Source = MagicMock()
        controller = MagicMock()
        evt2.Source.getCurrentController.return_value = controller
        listener.on_document_event(evt2)
        mock_reg.assert_called_once_with(controller)
