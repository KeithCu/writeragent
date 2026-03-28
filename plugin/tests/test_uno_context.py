import builtins
import sys
from unittest.mock import MagicMock, patch

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
    # Popping sys.modules["uno"] does not simulate missing pyuno: the next
    # `import uno` inside get_ctx() reloads from disk. Block import instead.
    mock_fallback = MagicMock()
    set_fallback_ctx(mock_fallback)
    orig_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "uno":
            raise ImportError("simulated missing uno")
        return orig_import(name, globals, locals, fromlist, level)

    try:
        with patch.object(builtins, "__import__", failing_import):
            assert get_ctx() == mock_fallback
    finally:
        set_fallback_ctx(None)

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
