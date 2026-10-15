# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for Writer change context menu disposal handling.

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
import pytest

from plugin.framework.uno_listeners import ListenerBoundary
from plugin.writer import change_context_menu
from plugin.writer.change_context_menu import _install_doc_event_listener


class DisposedException(Exception):
    pass


class RuntimeException(Exception):
    pass


@pytest.fixture(autouse=True)
def _reset_context_menu_state():
    orig_listener = change_context_menu._doc_listener
    orig_controllers = list(change_context_menu._registered_controllers)
    orig_interceptors = list(change_context_menu._interceptors)
    change_context_menu._doc_listener = None
    change_context_menu._registered_controllers.clear()
    change_context_menu._interceptors.clear()
    try:
        yield
    finally:
        change_context_menu._doc_listener = orig_listener
        change_context_menu._registered_controllers[:] = orig_controllers
        change_context_menu._interceptors[:] = orig_interceptors


def _build_listener() -> Any:
    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    _install_doc_event_listener(ctx)
    return broadcaster.addDocumentEventListener.call_args[0][0]


def test_doc_event_get_current_controller_disposal_is_listener_boundary():
    listener = _build_listener()
    source = MagicMock()
    source.getCurrentController.side_effect = DisposedException("gone")
    event = SimpleNamespace(EventName="OnLoad", ViewController=None, Source=source)

    with pytest.raises(ListenerBoundary) as exc_info:
        listener.documentEventOccured(event)
    assert exc_info.value.kind == "disposed"


def test_doc_event_runtime_exception_is_logged_not_boundary():
    listener = _build_listener()
    source = MagicMock()
    source.getCurrentController.side_effect = RuntimeException("live")
    event = SimpleNamespace(EventName="OnLoad", ViewController=None, Source=source)

    res = listener.documentEventOccured(event)
    assert res is None


def test_doc_event_get_model_disposal_is_listener_boundary():
    listener = _build_listener()
    vc = MagicMock()
    vc.getModel.side_effect = DisposedException("gone")
    event = SimpleNamespace(EventName="OnViewCreated", ViewController=vc, Source=None)

    with pytest.raises(ListenerBoundary) as exc_info:
        listener.documentEventOccured(event)
    assert exc_info.value.kind == "disposed"


def test_doc_event_supports_service_disposal_is_listener_boundary():
    listener = _build_listener()
    model = MagicMock()
    model.supportsService.side_effect = DisposedException("gone")
    vc = MagicMock()
    vc.getModel.return_value = model
    event = SimpleNamespace(EventName="OnViewCreated", ViewController=vc, Source=None)

    with pytest.raises(ListenerBoundary) as exc_info:
        listener.documentEventOccured(event)
    assert exc_info.value.kind == "disposed"

    # A supportsService that returns False returns None and does not raise (not a Writer).
    model.supportsService.side_effect = None
    model.supportsService.return_value = False
    res = listener.documentEventOccured(event)
    assert res is None


def test_unrelated_doc_event_returns():
    listener = _build_listener()
    source = MagicMock()
    event = SimpleNamespace(EventName="OnUnload", ViewController=None, Source=source)

    res = listener.documentEventOccured(event)
    assert res is None
    source.getCurrentController.assert_not_called()
