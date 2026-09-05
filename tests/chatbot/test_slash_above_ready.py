"""Above-Ready slash popup placement (no UNO)."""

from plugin.chatbot.slash_popup import (
    _above_ready_rect,
    _is_parent_local,
    _overlay_height,
)


class _R:
    def __init__(self, x, y, w, h):
        self.X, self.Y, self.Width, self.Height = x, y, w, h


def test_no_status_falls_back_above_ask():
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h = _above_ready_rect(qr, 6, status_top=None)
    assert (x, w) == (28, 1001)
    assert h == _overlay_height(6)
    assert y == 1853 - h - 2


def test_above_ready_uses_status_top():
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h = _above_ready_rect(qr, 6, status_top=1678)
    assert y == 1678 - _overlay_height(6) - 2
    assert y + h <= 1678


def test_parent_local_echo_detected():
    # Box bug: Ask accessible returned (8,360) while PosSize was ~9,361.
    assert _is_parent_local((8, 360), 9, 361, 307) is True
    assert _is_parent_local((940, 400), 9, 361, 307) is False
