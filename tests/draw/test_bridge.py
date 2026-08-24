"""Unit tests for DrawBridge chat-context helper (no UNO)."""

from unittest.mock import MagicMock, patch

from plugin.draw.bridge import get_draw_context_for_chat
from plugin.framework.errors import UnoObjectError


def test_get_draw_context_for_chat_disposed_returns_fallback():
    with patch("plugin.draw.bridge.check_disposed", side_effect=UnoObjectError("gone")):
        out = get_draw_context_for_chat(MagicMock())
    assert "Unable to read Draw/Impress context" in out


def test_get_draw_context_for_chat_summarizes_active_page():
    page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    pos = MagicMock(X=10, Y=20)
    size = MagicMock(Width=100, Height=50)
    shape.getPosition.return_value = pos
    shape.getSize.return_value = size
    shape.getString.return_value = "Hello"

    model = MagicMock()
    model.supportsService.return_value = False
    model.getURL.return_value = "file:///tmp/demo.odg"

    bridge = MagicMock()
    bridge.get_pages.return_value = pages
    bridge.get_active_page.return_value = page
    bridge.get_shapes.return_value = [shape]

    with (
        patch("plugin.draw.bridge.check_disposed"),
        patch("plugin.draw.bridge.DrawBridge", return_value=bridge),
        patch("plugin.draw.bridge.safe_call", side_effect=lambda fn, _msg, *args: fn(*args) if args else fn()),
    ):
        out = get_draw_context_for_chat(model, 8000)

    assert "Draw Document" in out
    assert "Total Pages: 1" in out
    assert "Hello" in out
