# tests/framework/test_uno_listeners.py
# Tests for unified UNO event listeners and stubs.

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


from plugin.framework.uno_listeners import (
    _catch_and_log,
    BaseListener,
    BaseActionListener,
    BaseItemListener,
    BaseTextListener,
    BaseKeyListener,
    BaseWindowListener,
    BaseTopWindowListener,
    BaseDocumentEventListener,
    BaseModifyListener,
    BaseTerminateListener,
    ListenerBoundary,
    install_view_doc_listener,
)

def test_catch_and_log_decorator(caplog):
    """Verify that @_catch_and_log swallows and logs unhandled exceptions."""
    class DummyListener:
        @_catch_and_log
        def someMethod(self, ev):
            raise RuntimeError("Test error inside listener")

    listener = DummyListener()
    
    # Executing the decorated method should not raise an exception
    with caplog.at_level(logging.ERROR):
        listener.someMethod(MagicMock())
        
    assert len(caplog.records) == 1
    assert "Test error inside listener" in caplog.text
    assert "DummyListener unhandled exception in someMethod" in caplog.text


def test_base_listener_disposing():
    """Verify that disposing is callable and safe."""
    listener = BaseListener()
    # Should run with no errors
    listener.disposing(MagicMock())


@pytest.mark.parametrize(
    ("base_cls", "uno_method", "hook_name"),
    [
        (BaseActionListener, "actionPerformed", "on_action_performed"),
        (BaseItemListener, "itemStateChanged", "on_item_state_changed"),
        (BaseTextListener, "textChanged", "on_text_changed"),
        (BaseKeyListener, "keyPressed", "on_key_pressed"),
        (BaseKeyListener, "keyReleased", "on_key_released"),
        (BaseWindowListener, "windowResized", "on_window_resized"),
        (BaseWindowListener, "windowMoved", "on_window_moved"),
        (BaseWindowListener, "windowShown", "on_window_shown"),
        (BaseWindowListener, "windowHidden", "on_window_hidden"),
        (BaseTopWindowListener, "windowClosing", "on_window_closing"),
        (BaseDocumentEventListener, "documentEventOccured", "on_document_event"),
    ],
)
def test_base_listener_dispatches_callback(base_cls, uno_method: str, hook_name: str):
    """Verify UNO listener methods invoke the matching subclass hook with the event."""
    received = []
    sub_cls = type(
        f"My{base_cls.__name__}",
        (base_cls,),
        {hook_name: lambda self, ev: received.append(ev)},
    )
    listener = sub_cls()
    ev = MagicMock()
    getattr(listener, uno_method)(ev)
    assert received == [ev]


def test_base_top_window_listener_other_events_are_noops():
    """Verify non-closing top-window UNO events do not invoke on_window_closing."""
    closing_called = False

    class MyTopWindowListener(BaseTopWindowListener):
        def on_window_closing(self, rEvent):
            nonlocal closing_called
            closing_called = True

    listener = MyTopWindowListener()
    ev = MagicMock()
    for method in ("windowOpened", "windowClosed", "windowMinimized", "windowNormalized", "windowActivated", "windowDeactivated"):
        getattr(listener, method)(ev)
    assert closing_called is False


@patch("plugin.framework.uno_listeners.log")
def test_base_action_listener_typed_exceptions(mock_log):
    """Exception paths log distinct messages for TypeError / ValueError / other."""

    class TypeErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise TypeError("Test type error")

    class ValueErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise ValueError("Test value error")

    class GenericErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise Exception("Test generic error")

    TypeErrListener().actionPerformed(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TypeErrListener TypeError in actionPerformed"

    ValueErrListener().actionPerformed(MagicMock())
    assert mock_log.exception.call_args[0][0] == "ValueErrListener ValueError in actionPerformed"

    GenericErrListener().actionPerformed(MagicMock())
    assert mock_log.exception.call_args[0][0] == "GenericErrListener unhandled exception in actionPerformed"


@pytest.mark.parametrize(
    ("base_cls", "uno_method", "hook_name"),
    [
        (BaseItemListener, "itemStateChanged", "on_item_state_changed"),
        (BaseTextListener, "textChanged", "on_text_changed"),
        (BaseWindowListener, "windowResized", "on_window_resized"),
        (BaseWindowListener, "windowMoved", "on_window_moved"),
        (BaseWindowListener, "windowShown", "on_window_shown"),
        (BaseWindowListener, "windowHidden", "on_window_hidden"),
    ],
)
@patch("plugin.framework.uno_listeners.log")
def test_base_listener_unhandled_exceptions(mock_log, base_cls, uno_method: str, hook_name: str):
    def boom(self, ev):
        raise Exception(f"boom in {uno_method}")

    sub_cls = type(f"Test{base_cls.__name__}", (base_cls,), {hook_name: boom})
    getattr(sub_cls(), uno_method)(MagicMock())
    assert mock_log.exception.call_args[0][0] == f"Test{base_cls.__name__} unhandled exception in {uno_method}"


def test_mouse_click_failure_returns_false():
    from plugin.framework.uno_listeners import BaseMouseClickHandler

    class Boom(BaseMouseClickHandler):
        def on_mouse_pressed(self, e):
            raise RuntimeError("nope")

    assert Boom().mousePressed(MagicMock()) is False


def test_disposed_exception_does_not_escape_listener():
    class DisposedException(Exception):
        pass

    class Gone(BaseListener):
        def on_disposing(self, source):
            raise DisposedException("gone")

    Gone().disposing(MagicMock())


def test_close_veto_still_reaches_the_bridge():
    class CloseVetoException(Exception):
        pass

    class Veto(BaseListener):
        def on_disposing(self, source):
            raise CloseVetoException("veto")

    try:
        Veto().disposing(MagicMock())
    except CloseVetoException:
        return
    raise AssertionError("CloseVetoException was swallowed")


def test_termination_veto_still_reaches_the_bridge():
    class TerminationVetoException(Exception):
        pass

    class Veto(BaseListener):
        def on_disposing(self, source):
            raise TerminationVetoException("veto quit")

    try:
        Veto().disposing(MagicMock())
    except TerminationVetoException:
        return
    raise AssertionError("TerminationVetoException was swallowed")


def test_subclass_disposing_override_is_wrapped():
    class Raw(BaseListener):
        def disposing(self, Source):
            raise RuntimeError("before try")

    Raw().disposing(MagicMock())


def test_subclass_item_state_changed_override_is_wrapped():
    """Settings/MCP listeners override itemStateChanged on the class dict."""
    from plugin.framework.uno_listeners import BaseItemListener

    class Raw(BaseItemListener):
        def itemStateChanged(self, rEvent):
            raise RuntimeError("before try")

    Raw().itemStateChanged(MagicMock())


def _thread_violation() -> RuntimeError:
    return RuntimeError("UNO thread violation: 'desktop' touched UNO from background task 'bg'")


def test_listener_boundary_is_not_an_exception():
    """A generic handler must not be able to name this type as Exception."""
    from plugin.framework.uno_listeners import ListenerBoundary

    assert issubclass(ListenerBoundary, BaseException)
    assert not issubclass(ListenerBoundary, Exception)


def test_generic_handler_cannot_swallow_thread_boundary():
    """except Exception used to hide assert_main_thread inside a callback."""

    class Boom(BaseActionListener):
        def on_action_performed(self, ev):
            raise _thread_violation()

    try:
        try:
            Boom().actionPerformed(MagicMock())
        except Exception as exc:
            raise AssertionError("generic handler swallowed the boundary") from exc
    except ListenerBoundary as boundary:
        assert boundary.kind == "thread"
        assert "UNO thread violation" in str(boundary.original)
        return
    raise AssertionError("thread violation left the listener as an empty callback")


def test_runtime_error_is_not_disposal():
    """A live-document RuntimeException must not become the disposed boundary."""

    class RuntimeException(Exception):
        pass

    class Boom(BaseActionListener):
        def on_action_performed(self, ev):
            raise RuntimeException("bad cursor on a live document")

    try:
        result = Boom().actionPerformed(MagicMock())
    except ListenerBoundary as boundary:
        raise AssertionError(f"runtime error reported as {boundary.kind}") from boundary
    assert result is None


def test_disposed_desktop_is_not_an_empty_document():
    """A dead desktop must not come back as the 'nothing is open' None."""
    from plugin.framework.uno_listeners import ListenerBoundary

    class DisposedException(Exception):
        pass

    class Read(BaseActionListener):
        def on_action_performed(self, ev):
            from plugin.framework.uno_context import get_active_document

            return get_active_document(MagicMock())

    result = None
    with patch("plugin.framework.uno_context.get_desktop", side_effect=DisposedException("desktop gone")):
        try:
            try:
                result = Read().actionPerformed(MagicMock())
            except Exception as exc:
                raise AssertionError("generic handler swallowed a disposed desktop") from exc
        except ListenerBoundary as boundary:
            assert boundary.kind == "disposed"
            return
    raise AssertionError(f"disposed desktop reported as an empty document: {result!r}")


def test_empty_document_is_not_disposed():
    """No current component is None, not a disposed desktop."""
    from plugin.framework.uno_listeners import ListenerBoundary

    class Read(BaseActionListener):
        def on_action_performed(self, ev):
            from plugin.framework.uno_context import get_active_document

            return get_active_document(MagicMock())

    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        try:
            result = Read().actionPerformed(MagicMock())
        except ListenerBoundary as boundary:
            raise AssertionError(f"empty document reported as {boundary.kind}") from boundary
    assert result is None


def test_disposal_typeerror_is_not_a_soft_typeerror():
    """TypeError used to be handled before listener_boundary.

    A disposal whose type subclasses TypeError must not be logged as an
    ordinary TypeError and returned as None.
    """

    class DisposedException(TypeError):
        pass

    class Boom(BaseActionListener):
        def on_action_performed(self, ev):
            raise DisposedException("gone")

    try:
        try:
            returned = Boom().actionPerformed(MagicMock())
        except Exception as exc:
            raise AssertionError("generic handler swallowed disposal") from exc
    except ListenerBoundary as boundary:
        assert boundary.kind == "disposed"
        return
    raise AssertionError(f"DisposedException(TypeError) was a soft failure: {returned!r}")


def test_disposing_typeerror_disposal_stays_inside_the_callback():
    """disposing still must not raise on real disposal, even if the type is a TypeError."""

    class DisposedException(TypeError):
        pass

    class Gone(BaseListener):
        def on_disposing(self, source):
            raise DisposedException("gone")

    Gone().disposing(MagicMock())


def test_veto_valueerror_is_the_original_type():
    """A veto that subclasses ValueError is re-raised as that UNO type."""

    class CloseVetoException(ValueError):
        pass

    class Veto(BaseListener):
        def on_disposing(self, source):
            raise CloseVetoException("veto")

    try:
        Veto().disposing(MagicMock())
    except CloseVetoException as exc:
        assert type(exc) is CloseVetoException
        return
    except ListenerBoundary as boundary:
        raise AssertionError(f"veto left as ListenerBoundary {boundary.kind}") from boundary
    raise AssertionError("CloseVetoException(ValueError) was swallowed")


def test_click_thread_violation_is_not_a_rejected_click():
    from plugin.framework.uno_listeners import BaseMouseClickHandler, ListenerBoundary

    class Boom(BaseMouseClickHandler):
        def on_mouse_pressed(self, e):
            raise _thread_violation()

    with_failure = False
    try:
        try:
            returned = Boom().mousePressed(MagicMock())
            with_failure = returned is False
        except Exception as exc:
            raise AssertionError("generic handler swallowed the boundary") from exc
    except ListenerBoundary as boundary:
        assert boundary.kind == "thread"
        return
    if with_failure:
        raise AssertionError("thread violation looked like mousePressed returning False")
    raise AssertionError("thread violation did not leave the click handler")


def test_mouse_click_handler_unannotated_override_returns_false() -> None:
    """Subclass overriding mousePressed/mouseReleased without annotations returns False on failure."""
    from plugin.framework.uno_listeners import BaseMouseClickHandler

    class UnannotatedOverride(BaseMouseClickHandler):
        def mousePressed(self, e):
            raise RuntimeError("click failed")

        def mouseReleased(self, e):
            raise RuntimeError("release failed")

    handler = UnannotatedOverride()
    assert handler.mousePressed(MagicMock()) is False
    assert handler.mouseReleased(MagicMock()) is False


def test_key_handler_default_is_false() -> None:
    from plugin.framework.uno_listeners import BaseKeyHandler

    handler = BaseKeyHandler()
    assert handler.keyPressed(MagicMock()) is False
    assert handler.keyReleased(MagicMock()) is False
    handler.disposing(MagicMock())


def test_key_handler_failure_returns_false() -> None:
    from plugin.framework.uno_listeners import BaseKeyHandler

    class Boom(BaseKeyHandler):
        def on_key_pressed(self, e):
            raise RuntimeError("nope")

    assert Boom().keyPressed(MagicMock()) is False


def test_key_handler_thread_violation_is_not_a_rejected_key() -> None:
    from plugin.framework.uno_listeners import BaseKeyHandler, ListenerBoundary

    class Boom(BaseKeyHandler):
        def on_key_pressed(self, e):
            raise _thread_violation()

    with_failure = False
    try:
        try:
            returned = Boom().keyPressed(MagicMock())
            with_failure = returned is False
        except Exception as exc:
            raise AssertionError("generic handler swallowed the boundary") from exc
    except ListenerBoundary as boundary:
        assert boundary.kind == "thread"
        return
    if with_failure:
        raise AssertionError("thread violation looked like keyPressed returning False")
    raise AssertionError("thread violation did not leave the key handler")


def test_key_handler_unannotated_override_returns_false() -> None:
    """Subclass overriding keyPressed/keyReleased without annotations returns False on failure."""
    from plugin.framework.uno_listeners import BaseKeyHandler

    class UnannotatedOverride(BaseKeyHandler):
        def keyPressed(self, e):
            raise RuntimeError("key press failed")

        def keyReleased(self, e):
            raise RuntimeError("key release failed")

    handler = UnannotatedOverride()
    assert handler.keyPressed(MagicMock()) is False
    assert handler.keyReleased(MagicMock()) is False


def test_mouse_listener_unannotated_override_returns_none() -> None:
    """Subclass overriding mousePressed on BaseMouseListener returns None on failure."""
    from plugin.framework.uno_listeners import BaseMouseListener

    class UnannotatedMouse(BaseMouseListener):
        def mousePressed(self, e):
            raise RuntimeError("mouse pressed failed")

    listener = UnannotatedMouse()
    assert listener.mousePressed(MagicMock()) is None


def test_new_wrapped_callback_is_applied_to_overrides():
    """Subclass overriding a custom @_catch_and_log method is dynamically wrapped."""
    class BaseExtra(BaseListener):
        @_catch_and_log
        def extraPing(self, ev):
            self.on_extra_ping(ev)

        def on_extra_ping(self, ev):
            pass

    class Child(BaseExtra):
        def extraPing(self, ev):
            raise RuntimeError("extra ping failed")

    # Child().extraPing(None) must not raise because __init_subclass__ wrapped extraPing dynamically from MRO
    Child().extraPing(None)


def test_install_view_doc_listener() -> None:
    """Verify install_view_doc_listener attaches to GlobalEventBroadcaster and handles events."""
    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster

    registered: list[Any] = []
    listener = install_view_doc_listener(ctx, registered.append, "test_listener")
    assert listener is not None
    smgr.createInstanceWithContext.assert_called_once_with(
        "com.sun.star.frame.GlobalEventBroadcaster", ctx
    )
    broadcaster.addDocumentEventListener.assert_called_once_with(listener)

    # Irrelevant event is ignored
    evt = MagicMock()
    evt.EventName = "OnSave"
    listener.on_document_event(evt)
    assert registered == []

    # OnViewCreated with ViewController invokes register callback
    ctrl = MagicMock()
    evt_view = MagicMock()
    evt_view.EventName = "OnViewCreated"
    evt_view.ViewController = ctrl
    listener.on_document_event(evt_view)
    assert registered == [ctrl]

    # OnLoadFinished without ViewController falls back to Source.getCurrentController
    ctrl2 = MagicMock()
    source = MagicMock()
    source.getCurrentController.return_value = ctrl2
    evt_load = MagicMock(spec=["EventName", "Source"])
    evt_load.EventName = "OnLoadFinished"
    evt_load.Source = source
    listener.on_document_event(evt_load)
    assert registered == [ctrl, ctrl2]


def test_install_view_doc_listener_handles_broadcaster_failure() -> None:
    """Verify install_view_doc_listener returns None when broadcaster creation fails."""
    ctx = MagicMock()
    ctx.getServiceManager.side_effect = RuntimeError("Service manager unavailable")
    result = install_view_doc_listener(ctx, lambda c: None, "test_failure")
    assert result is None


def test_install_view_doc_listener_on_unload() -> None:
    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster

    unloaded: list[Any] = []
    listener = install_view_doc_listener(
        ctx,
        lambda c: None,
        "test_unload",
        on_unload=unloaded.append,
    )
    assert listener is not None

    # Callback receives Event.Source when ViewController is absent
    model1 = MagicMock()
    evt1 = MagicMock(spec=["EventName", "ViewController", "Source"])
    evt1.EventName = "OnUnload"
    evt1.ViewController = None
    evt1.Source = model1
    listener.on_document_event(evt1)
    assert unloaded == [model1]

    # Callback receives ViewController.getModel() when present
    model2 = MagicMock()
    vc = MagicMock()
    vc.getModel.return_value = model2
    evt2 = MagicMock(spec=["EventName", "ViewController", "Source"])
    evt2.EventName = "OnUnload"
    evt2.ViewController = vc
    evt2.Source = MagicMock()
    listener.on_document_event(evt2)
    assert unloaded == [model1, model2]


def test_install_view_doc_listener_ignores_unload_without_callback() -> None:
    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster

    registered: list[Any] = []
    listener = install_view_doc_listener(ctx, registered.append, "test_no_unload")
    assert listener is not None

    source = MagicMock()
    evt = MagicMock(spec=["EventName", "ViewController", "Source"])
    evt.EventName = "OnUnload"
    evt.ViewController = None
    evt.Source = source
    listener.on_document_event(evt)

    assert registered == []
    source.getCurrentController.assert_not_called()


def test_base_modify_listener():
    """Verify modify listener invokes the correct subclass callback and catches exceptions."""
    class MyModifyListener(BaseModifyListener):
        def __init__(self):
            super().__init__()
            self.called = False
            self.event = None

        def on_modified(self, aEvent):
            self.called = True
            self.event = aEvent

    listener = MyModifyListener()
    ev = MagicMock()
    listener.modified(ev)
    assert listener.called is True
    assert listener.event == ev

    # Direct override is discovered by __init_subclass__ and wrapped
    class DirectOverrideModifyListener(BaseModifyListener):
        def modified(self, aEvent):
            raise RuntimeError("modify crash")

    # Should catch and log, not raise
    DirectOverrideModifyListener().modified(ev)


@patch("plugin.framework.uno_listeners.log")
def test_base_modify_listener_exceptions(mock_log):
    class ErrorModifyListener(BaseModifyListener):
        def on_modified(self, aEvent):
            raise ValueError("bad modify value")

    ErrorModifyListener().modified(MagicMock())
    assert mock_log.exception.call_args[0][0] == "ErrorModifyListener ValueError in modified"


def test_base_terminate_listener():
    """Verify terminate listener invokes callbacks and preserves veto exceptions."""
    class MyTerminateListener(BaseTerminateListener):
        def __init__(self):
            super().__init__()
            self.query_called = False
            self.notify_called = False

        def on_query_termination(self, Event):
            self.query_called = True

        def on_notify_termination(self, Event):
            self.notify_called = True

    listener = MyTerminateListener()
    ev = MagicMock()
    listener.queryTermination(ev)
    listener.notifyTermination(ev)
    assert listener.query_called is True
    assert listener.notify_called is True

    # Termination veto is re-raised to the bridge
    class TerminationVetoException(Exception):
        pass

    class VetoTerminateListener(BaseTerminateListener):
        def on_query_termination(self, Event):
            raise TerminationVetoException("app cannot terminate")

    try:
        VetoTerminateListener().queryTermination(ev)
    except TerminationVetoException:
        pass
    else:
        raise AssertionError("TerminationVetoException was swallowed by BaseTerminateListener")


@patch("plugin.framework.uno_listeners.log")
def test_base_terminate_listener_exceptions(mock_log):
    class ErrorTerminateListener(BaseTerminateListener):
        def on_notify_termination(self, Event):
            raise RuntimeError("crash on notify")

    ErrorTerminateListener().notifyTermination(MagicMock())
    assert mock_log.exception.call_args[0][0] == "ErrorTerminateListener unhandled exception in notifyTermination"




