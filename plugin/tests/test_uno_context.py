import sys
from unittest.mock import MagicMock

from plugin.framework.uno_context import set_fallback_ctx, get_ctx

def test_get_ctx_with_uno():
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx

    # Temporarily override the real module
    sys.modules['uno'] = mock_uno

    try:
        assert get_ctx() == mock_ctx
        mock_uno.getComponentContext.assert_called_once()
    finally:
        # Restore sys.modules to its original state if it wasn't a mock
        # We delete it because it shouldn't exist in our test environment normally,
        # but if the test suite set it up, it's safer to pop it.
        sys.modules.pop('uno', None)


def test_get_ctx_fallback():
    # Make sure 'uno' is not in sys.modules to simulate ImportError
    original_uno = sys.modules.pop('uno', None)

    # The issue is that the `get_ctx` function might still find `uno` in `sys.modules` if
    # `uno` is imported somewhere else during the test run, or if the fallback context
    # is returned instead. We should mock the `__import__` built-in or ensure the local
    # scope's import raises ImportError. We can patch `sys.modules` but `get_ctx` might
    # re-import it. To reliably simulate an environment without `uno`, we can map it to None.
    sys.modules['uno'] = None

    try:
        mock_fallback = MagicMock()
        set_fallback_ctx(mock_fallback)

        assert get_ctx() == mock_fallback
    finally:
        # cleanup
        set_fallback_ctx(None)
        if original_uno is not None:
            sys.modules['uno'] = original_uno
        else:
            sys.modules.pop('uno', None)

def test_get_ctx_fallback_uno_returns_none():
    mock_uno = MagicMock()
    mock_uno.getComponentContext.return_value = None
    sys.modules['uno'] = mock_uno

    try:
        mock_fallback = MagicMock()
        set_fallback_ctx(mock_fallback)
        assert get_ctx() == mock_fallback
    finally:
        sys.modules.pop('uno', None)
        set_fallback_ctx(None)
