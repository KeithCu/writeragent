# tests/framework/test_uno_listeners.py
# Tests for unified UNO event listeners and stubs.

import logging
from unittest.mock import MagicMock, patch


from plugin.framework.uno_listeners import (
    _catch_and_log,
    BaseListener,
    BaseActionListener,
    BaseItemListener,
    BaseTextListener,
    BaseKeyListener,
    BaseWindowListener,
    BaseDocumentEventListener,
    ListenerBoundary,
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


def test_base_action_listener():
    """Verify action listener invokes the correct subclass callback."""
    class MyActionListener(BaseActionListener):
        def __init__(self):
            super().__init__()
            self.called = False
            self.event = None

        def on_action_performed(self, rEvent):
            self.called = True
            self.event = rEvent

    listener = MyActionListener()
    ev = MagicMock()
    listener.actionPerformed(ev)
    assert listener.called is True
    assert listener.event == ev


def test_base_item_listener():
    """Verify item listener invokes the correct subclass callback."""
    class MyItemListener(BaseItemListener):
        def __init__(self):
            super().__init__()
            self.called = False

        def on_item_state_changed(self, rEvent):
            self.called = True

    listener = MyItemListener()
    listener.itemStateChanged(MagicMock())
    assert listener.called is True


def test_base_text_listener():
    """Verify text listener invokes the correct subclass callback."""
    class MyTextListener(BaseTextListener):
        def __init__(self):
            super().__init__()
            self.called = False

        def on_text_changed(self, rEvent):
            self.called = True

    listener = MyTextListener()
    listener.textChanged(MagicMock())
    assert listener.called is True


def test_base_key_listener():
    """Verify key listener invokes the correct subclass callback."""
    class MyKeyListener(BaseKeyListener):
        def __init__(self):
            super().__init__()
            self.pressed = False
            self.released = False

        def on_key_pressed(self, e):
            self.pressed = True

        def on_key_released(self, e):
            self.released = True

    listener = MyKeyListener()
    listener.keyPressed(MagicMock())
    listener.keyReleased(MagicMock())
    assert listener.pressed is True
    assert listener.released is True


def test_base_window_listener():
    """Verify window listener invokes all resize/move/show/hide callbacks."""
    class MyWindowListener(BaseWindowListener):
        def __init__(self):
            super().__init__()
            self.resized = False
            self.moved = False
            self.shown = False
            self.hidden = False

        def on_window_resized(self, rEvent):
            self.resized = True

        def on_window_moved(self, rEvent):
            self.moved = True

        def on_window_shown(self, rEvent):
            self.shown = True

        def on_window_hidden(self, rEvent):
            self.hidden = True

    listener = MyWindowListener()
    ev = MagicMock()
    listener.windowResized(ev)
    listener.windowMoved(ev)
    listener.windowShown(ev)
    listener.windowHidden(ev)

    assert listener.resized is True
    assert listener.moved is True
    assert listener.shown is True
    assert listener.hidden is True


def test_base_document_event_listener():
    """Verify document event listener invokes the correct subclass callback."""
    class MyDocListener(BaseDocumentEventListener):
        def __init__(self):
            super().__init__()
            self.called = False
            self.event = None

        def on_document_event(self, Event):
            self.called = True
            self.event = Event

    listener = MyDocListener()
    ev = MagicMock()
    listener.documentEventOccured(ev)
    assert listener.called is True
    assert listener.event == ev


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


@patch("plugin.framework.uno_listeners.log")
def test_base_item_listener_exceptions(mock_log):
    class TestItemListener(BaseItemListener):
        def on_item_state_changed(self, ev):
            raise Exception("Test item generic error")

    TestItemListener().itemStateChanged(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestItemListener unhandled exception in itemStateChanged"


@patch("plugin.framework.uno_listeners.log")
def test_base_text_listener_exceptions(mock_log):
    class TestTextListener(BaseTextListener):
        def on_text_changed(self, ev):
            raise Exception("Test text generic error")

    TestTextListener().textChanged(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestTextListener unhandled exception in textChanged"


@patch("plugin.framework.uno_listeners.log")
def test_base_window_listener_exceptions(mock_log):
    class TestWindowListener(BaseWindowListener):
        def on_window_resized(self, ev):
            raise Exception("Test window resized error")

        def on_window_moved(self, ev):
            raise Exception("Test window moved error")

        def on_window_shown(self, ev):
            raise Exception("Test window shown error")

        def on_window_hidden(self, ev):
            raise Exception("Test window hidden error")

    listener = TestWindowListener()
    listener.windowResized(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestWindowListener unhandled exception in windowResized"
    listener.windowMoved(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestWindowListener unhandled exception in windowMoved"
    listener.windowShown(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestWindowListener unhandled exception in windowShown"
    listener.windowHidden(MagicMock())
    assert mock_log.exception.call_args[0][0] == "TestWindowListener unhandled exception in windowHidden"


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
