"""Unit tests for menu icon DPI probe interpolation (no UNO)."""

from plugin.framework.menu_icon_dpi import (
    interpolate_menu_icon_px,
    reset_menu_icon_dpi_cache,
)


def setup_function(_fn=None):
    reset_menu_icon_dpi_cache()


def test_hidpi_scale_keeps_16():
    assert interpolate_menu_icon_px(2.0) == 16


def test_one_x_prefers_32():
    assert interpolate_menu_icon_px(1.0) == 32


def test_mid_scale_snaps_to_26():
    assert interpolate_menu_icon_px(1.5) == 26


def test_ultra_hidpi_stays_at_least_16():
    assert interpolate_menu_icon_px(3.0) == 16


def test_failed_probe_defaults_to_hidpi_safe_16(monkeypatch):
    from plugin.framework import menu_icon_dpi as m

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    assert m.resolve_menu_icon_pixel_size() == 16
