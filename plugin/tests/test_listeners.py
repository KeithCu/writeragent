import sys
import pytest
from unittest.mock import MagicMock, patch

# Provide mock implementations for the base classes needed by listeners.py.
# This prevents metaclass conflicts and "duplicate base class" errors.
class MockUnohelperBase:
    pass

class MockXEventListener:
    pass

class MockXActionListener:
    pass

class MockXItemListener:
    pass

class MockXTextListener:
    pass

class MockXWindowListener:
    pass


class MockXKeyListener:
    pass


@pytest.fixture(autouse=True)
def mock_uno_modules():
    """Mocks the necessary UNO modules and classes for the tests using patch.dict.
    Restores the original modules after the test to prevent pollution.
    """
    mock_uno = MagicMock()

    mock_unohelper_module = MagicMock()
    mock_unohelper_module.Base = MockUnohelperBase

    mock_awt_module = MagicMock()
    mock_awt_module.XActionListener = MockXActionListener
    mock_awt_module.XItemListener = MockXItemListener
    mock_awt_module.XTextListener = MockXTextListener
    mock_awt_module.XWindowListener = MockXWindowListener
    mock_awt_module.XKeyListener = MockXKeyListener

    mock_lang_module = MagicMock()
    mock_lang_module.XEventListener = MockXEventListener

    # Use patch.dict to safely replace sys.modules
    with patch.dict('sys.modules', {
        'uno': mock_uno,
        'unohelper': mock_unohelper_module,
        'com.sun.star.awt': mock_awt_module,
        'com.sun.star.lang': mock_lang_module
    }):
        # We must reload the listeners module so it picks up the mocks
        if 'plugin.framework.listeners' in sys.modules:
            del sys.modules['plugin.framework.listeners']

        yield

        # Also clean up the mocked version of listeners module to prevent polluting other tests
        if 'plugin.framework.listeners' in sys.modules:
            del sys.modules['plugin.framework.listeners']

def test_base_listener_disposing():
    from plugin.framework.listeners import BaseListener
    listener = BaseListener()
    # disposing should not raise any exceptions
    listener.disposing(MagicMock())

@patch("plugin.framework.listeners.log")
def test_base_action_listener_success(mock_log):
    from plugin.framework.listeners import BaseActionListener
    class TestActionListener(BaseActionListener):
        def on_action_performed(self, ev):
            self.success = True

    listener = TestActionListener()
    listener.actionPerformed(MagicMock())
    assert listener.success
    mock_log.error.assert_not_called()

@patch("plugin.framework.listeners.log")
def test_base_action_listener_exceptions(mock_log):
    from plugin.framework.listeners import BaseActionListener
    class TypeErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise TypeError("Test type error")

    class ValueErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise ValueError("Test value error")

    class GenericErrListener(BaseActionListener):
        def on_action_performed(self, ev):
            raise Exception("Test generic error")

    # Test TypeError
    TypeErrListener().actionPerformed(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "TypeErrListener TypeError in actionPerformed: %s" == args[0]
    assert isinstance(args[1], TypeError)

    # Test ValueError
    ValueErrListener().actionPerformed(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "ValueErrListener ValueError in actionPerformed: %s" == args[0]
    assert isinstance(args[1], ValueError)

    # Test Generic Exception
    GenericErrListener().actionPerformed(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "GenericErrListener unhandled exception in actionPerformed: %s" == args[0]
    assert isinstance(args[1], Exception)
    assert kwargs.get("exc_info") is True

@patch("plugin.framework.listeners.log")
def test_base_item_listener_exceptions(mock_log):
    from plugin.framework.listeners import BaseItemListener
    class TestItemListener(BaseItemListener):
        def on_item_state_changed(self, ev):
            raise Exception("Test item generic error")

    listener = TestItemListener()
    listener.itemStateChanged(MagicMock())

    args, kwargs = mock_log.error.call_args
    assert "TestItemListener unhandled exception in itemStateChanged: %s" == args[0]
    assert isinstance(args[1], Exception)
    assert kwargs.get("exc_info") is True

@patch("plugin.framework.listeners.log")
def test_base_text_listener_exceptions(mock_log):
    from plugin.framework.listeners import BaseTextListener
    class TestTextListener(BaseTextListener):
        def on_text_changed(self, ev):
            raise Exception("Test text generic error")

    listener = TestTextListener()
    listener.textChanged(MagicMock())

    args, kwargs = mock_log.error.call_args
    assert "TestTextListener unhandled exception in textChanged: %s" == args[0]
    assert isinstance(args[1], Exception)
    assert kwargs.get("exc_info") is True

@patch("plugin.framework.listeners.log")
def test_base_window_listener_exceptions(mock_log):
    from plugin.framework.listeners import BaseWindowListener
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

    # Test windowResized
    listener.windowResized(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "TestWindowListener unhandled exception in windowResized: %s" == args[0]

    # Test windowMoved
    listener.windowMoved(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "TestWindowListener unhandled exception in windowMoved: %s" == args[0]

    # Test windowShown
    listener.windowShown(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "TestWindowListener unhandled exception in windowShown: %s" == args[0]

    # Test windowHidden
    listener.windowHidden(MagicMock())
    args, kwargs = mock_log.error.call_args
    assert "TestWindowListener unhandled exception in windowHidden: %s" == args[0]
