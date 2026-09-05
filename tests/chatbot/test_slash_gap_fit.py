"""Gap-fit slash popup placement (no UNO)."""

from plugin.chatbot.slash_popup import _gap_fit_overlay_rect


class _R:
    def __init__(self, x, y, w, h):
        self.X, self.Y, self.Width, self.Height = x, y, w, h


def test_no_status_keeps_classic_above_ask():
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h, rows = _gap_fit_overlay_rect(qr, 6, status_bottom=None)
    assert (x, w) == (28, 1001)
    assert y == 1853 - 90 - 2
    assert h == 90
    assert rows == 6


def test_status_overlap_fits_below_ready():
    # Keith Arch: status bottom 1768, Ask Y 1853, desired 90 overlaps Ready.
    qr = _R(28, 1853, 1001, 206)
    x, y, w, h, rows = _gap_fit_overlay_rect(qr, 6, status_bottom=1768)
    assert y == 1768
    assert y + h <= 1853 - 2
    assert rows >= 1
    assert rows < 6 or h <= 1853 - 1768 - 2
