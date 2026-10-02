"""Unit tests for the production live-panel uid map."""

from plugin.doc.live_panels import (
    get_live_panel,
    iter_live_panel_uids,
    register_live_panel,
    reset_live_panels,
    unregister_live_panel,
)


class _Panel:
    def __init__(self, name):
        self.name = name


def setup_function():
    reset_live_panels()


def teardown_function():
    reset_live_panels()


def test_register_and_get():
    panel = _Panel("a")
    register_live_panel("uid-1", panel)
    assert get_live_panel("uid-1") is panel
    assert "uid-1" in iter_live_panel_uids()


def test_skip_empty_uid():
    register_live_panel("", _Panel("x"))
    assert iter_live_panel_uids() == []
    assert get_live_panel("") is None


def test_last_write_wins():
    first = _Panel("first")
    second = _Panel("second")
    register_live_panel("uid-1", first)
    register_live_panel("uid-1", second)
    assert get_live_panel("uid-1") is second


def test_unregister():
    panel = _Panel("a")
    register_live_panel("uid-1", panel)
    unregister_live_panel("uid-1")
    assert get_live_panel("uid-1") is None


def test_unregister_leaves_a_newer_panel():
    first = _Panel("first")
    second = _Panel("second")
    register_live_panel("uid-1", first)
    register_live_panel("uid-1", second)
    unregister_live_panel("uid-1", first)
    assert get_live_panel("uid-1") is second
    unregister_live_panel("uid-1", second)
    assert get_live_panel("uid-1") is None


def test_weak_value_drops_when_unreferenced():
    register_live_panel("uid-1", _Panel("gone"))
    assert get_live_panel("uid-1") is None
