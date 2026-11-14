# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for Writer change context menu disposal handling.

import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
import pytest

from plugin.framework.uno_listeners import ListenerBoundary
from plugin.writer import change_context_menu, review_view_attachment as rva


def _registered_controllers(registry: dict[str, Any]) -> list[tuple[Any, Any]]:
    return [(c, i) for att in registry.values() for c, i, _h in att.controllers]


class DisposedException(Exception):
    pass


class RuntimeException(Exception):
    pass


@pytest.fixture(autouse=True)
def _reset_context_menu_state(review_registry: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(change_context_menu, "_interceptor_cls", None)
    return review_registry


def _build_listener() -> Any:
    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    rva.install(ctx)
    return broadcaster.addDocumentEventListener.call_args[0][0]


def test_doc_event_get_current_controller_disposal_is_listener_boundary():
    listener = _build_listener()
    source = MagicMock()
    source.getCurrentController.side_effect = DisposedException("gone")
    event = SimpleNamespace(EventName="OnLoad", ViewController=None, Source=source)

    with pytest.raises(ListenerBoundary) as exc_info:
        listener.documentEventOccured(event)
    assert exc_info.value.kind == "disposed"


def test_doc_event_runtime_exception_returns_none_not_boundary():
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


class _StubController:
    def __init__(self, ident: str, model: Any) -> None:
        self.ident = ident
        self._model = model
        self.registered: list[Any] = []
        self.removed: list[Any] = []

    def getModel(self) -> Any:  # noqa: N802 -- UNO API
        return self._model

    def registerContextMenuInterceptor(self, interceptor: Any) -> None:  # noqa: N802 -- UNO API
        self.registered.append(interceptor)

    def releaseContextMenuInterceptor(self, interceptor: Any) -> None:  # noqa: N802 -- UNO API
        self.removed.append(interceptor)

    def addMouseClickHandler(self, handler: Any) -> None:  # noqa: N802 -- UNO API
        pass

    def removeMouseClickHandler(self, handler: Any) -> None:  # noqa: N802 -- UNO API
        pass


def test_register_controller_dedups_by_uno_identity(monkeypatch, review_registry):
    monkeypatch.setattr(
        "plugin.writer.review_view_attachment.uno_same",
        lambda a, b: a is not None and b is not None and getattr(a, "ident", None) == getattr(b, "ident", None),
    )
    model = MagicMock()
    model.supportsService.return_value = True

    ctrl1 = _StubController("ctrl-1", model)
    ctrl2 = _StubController("ctrl-1", model)

    monkeypatch.setattr(
        change_context_menu,
        "make_interceptor",
        lambda m: SimpleNamespace(model=m),
    )
    monkeypatch.setattr(
        "plugin.writer.review_click_popup.ClickReviewHandler",
        lambda m, detach: MagicMock(),
    )

    rva.register_controller(ctrl1)
    assert len(ctrl1.registered) == 1
    assert len(_registered_controllers(review_registry)) == 1

    rva.register_controller(ctrl2)
    assert len(ctrl2.registered) == 0
    assert len(_registered_controllers(review_registry)) == 1


def _seed_controller_hooks(
    registry: dict[str, Any],
    controller: Any,
    interceptor: Any,
    click_handler: Any | None = None,
) -> None:
    handler = click_handler if click_handler is not None else MagicMock()
    model = getattr(interceptor, "model", None)
    key = rva.attachment_key(model) if model is not None else "test:%d" % id(controller)
    with rva._lock:
        attachment = registry.get(key)
        if attachment is None:
            attachment = rva.ReviewViewAttachment(key=key, model=model)
            registry[key] = attachment
        attachment.controllers.append((controller, interceptor, handler))


def test_unload_removes_interceptor_and_pair(review_registry: dict[str, Any]):
    model1 = SimpleNamespace(ident="m1")
    model2 = SimpleNamespace(ident="m2")

    ctrl1 = _StubController("c1", model1)
    ctrl2 = _StubController("c2", model2)

    interceptor1 = SimpleNamespace(model=model1)
    interceptor2 = SimpleNamespace(model=model2)

    _seed_controller_hooks(review_registry, ctrl1, interceptor1)
    _seed_controller_hooks(review_registry, ctrl2, interceptor2)

    rva.unregister_controllers_for_model(model1)
    assert ctrl1.removed == [interceptor1]
    assert ctrl2.removed == []
    assert _registered_controllers(review_registry) == [(ctrl2, interceptor2)]


def test_unload_swallows_removal_disposal(review_registry: dict[str, Any]):
    model = SimpleNamespace(ident="m")
    ctrl = _StubController("c", model)
    interceptor = SimpleNamespace(model=model)

    def _raise_gone(inter):
        raise RuntimeError("gone")

    ctrl.releaseContextMenuInterceptor = _raise_gone
    _seed_controller_hooks(review_registry, ctrl, interceptor)

    rva.unregister_controllers_for_model(model)
    assert _registered_controllers(review_registry) == []


def test_on_unload_event_removes_registered_interceptor(review_registry: dict[str, Any]):
    model = MagicMock()
    model.supportsService.return_value = True
    ctrl = _StubController("c", model)
    interceptor = SimpleNamespace(model=model)

    _seed_controller_hooks(review_registry, ctrl, interceptor)
    listener = _build_listener()

    event = SimpleNamespace(EventName="OnUnload", ViewController=None, Source=model)
    listener.documentEventOccured(event)
    assert ctrl.removed == [interceptor]
    assert _registered_controllers(review_registry) == []


@pytest.fixture
def uno_menu_stubs():
    saved_ui = sys.modules.get("com.sun.star.ui")
    saved_action = sys.modules.get("com.sun.star.ui.ContextMenuInterceptorAction")
    orig_interceptor_cls = change_context_menu._interceptor_cls

    ui_mod = types.ModuleType("com.sun.star.ui")
    ui_mod.__path__ = []

    class _StubXContextMenuInterceptor:
        pass

    class _StubSeparatorType:
        LINE = 0

    ui_mod.XContextMenuInterceptor = _StubXContextMenuInterceptor
    ui_mod.ActionTriggerSeparatorType = _StubSeparatorType

    action_mod = types.ModuleType("com.sun.star.ui.ContextMenuInterceptorAction")
    ignored_sentinel = object()
    continue_modified_sentinel = object()
    action_mod.IGNORED = ignored_sentinel
    action_mod.CONTINUE_MODIFIED = continue_modified_sentinel

    sys.modules["com.sun.star.ui"] = ui_mod
    sys.modules["com.sun.star.ui.ContextMenuInterceptorAction"] = action_mod
    change_context_menu._interceptor_cls = None

    try:
        yield SimpleNamespace(
            ignored=ignored_sentinel,
            continue_modified=continue_modified_sentinel,
        )
    finally:
        change_context_menu._interceptor_cls = orig_interceptor_cls
        if saved_ui is not None:
            sys.modules["com.sun.star.ui"] = saved_ui
        else:
            sys.modules.pop("com.sun.star.ui", None)
        if saved_action is not None:
            sys.modules["com.sun.star.ui.ContextMenuInterceptorAction"] = saved_action
        else:
            sys.modules.pop("com.sun.star.ui.ContextMenuInterceptorAction", None)


def test_build_menu_ignored_without_agent_changes(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (0, True),
    )
    container = MagicMock()
    event = SimpleNamespace(ActionTriggerContainer=container)
    res = change_context_menu._build_menu(
        MagicMock(), event, uno_menu_stubs.ignored, uno_menu_stubs.continue_modified
    )
    assert res is uno_menu_stubs.ignored
    container.createInstance.assert_not_called()


def test_build_menu_unreliable_scan_still_shows(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (0, False),
    )
    container = MagicMock()
    container.getCount.return_value = 0
    event = SimpleNamespace(ActionTriggerContainer=container)
    res = change_context_menu._build_menu(
        MagicMock(), event, uno_menu_stubs.ignored, uno_menu_stubs.continue_modified
    )
    assert res is uno_menu_stubs.continue_modified


def test_build_menu_inserts_separator_and_two_triggers(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (1, True),
    )
    separator = MagicMock(name="separator")
    accept = MagicMock(name="accept")
    reject = MagicMock(name="reject")

    def _create_instance(service_name: str) -> MagicMock:
        if service_name == "com.sun.star.ui.ActionTriggerSeparator":
            return separator
        if service_name == "com.sun.star.ui.ActionTrigger":
            if not accept.setPropertyValue.called:
                return accept
            return reject
        return MagicMock()

    container = MagicMock()
    container.getCount.return_value = 3
    container.createInstance.side_effect = _create_instance

    event = SimpleNamespace(ActionTriggerContainer=container)
    res = change_context_menu._build_menu(
        MagicMock(), event, uno_menu_stubs.ignored, uno_menu_stubs.continue_modified
    )
    assert res is uno_menu_stubs.continue_modified

    container.insertByIndex.assert_any_call(3, separator)
    container.insertByIndex.assert_any_call(4, accept)
    container.insertByIndex.assert_any_call(5, reject)

    separator.setPropertyValue.assert_called_with("SeparatorType", 0)
    accept.setPropertyValue.assert_any_call("CommandURL", change_context_menu._ACCEPT_URL)
    reject.setPropertyValue.assert_any_call("CommandURL", change_context_menu._REJECT_URL)


def test_build_menu_ignored_without_create_instance_container(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (1, True),
    )
    container = SimpleNamespace()
    event = SimpleNamespace(ActionTriggerContainer=container)
    res = change_context_menu._build_menu(
        MagicMock(), event, uno_menu_stubs.ignored, uno_menu_stubs.continue_modified
    )
    assert res is uno_menu_stubs.ignored


def test_build_menu_exception_returns_ignored(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (1, True),
    )
    container = MagicMock()
    container.createInstance.side_effect = RuntimeError("failure")
    event = SimpleNamespace(ActionTriggerContainer=container)
    res = change_context_menu._build_menu(
        MagicMock(), event, uno_menu_stubs.ignored, uno_menu_stubs.continue_modified
    )
    assert res is uno_menu_stubs.ignored


def test_interceptor_passes_uno_enums(uno_menu_stubs, monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.review_scan.agent_change_presence",
        lambda m, stop_at_first=True: (0, True),
    )
    model = MagicMock()
    interceptor = change_context_menu.make_interceptor(model)
    container = MagicMock()
    res = interceptor.notifyContextMenuExecute(SimpleNamespace(ActionTriggerContainer=container))
    assert res is uno_menu_stubs.ignored
