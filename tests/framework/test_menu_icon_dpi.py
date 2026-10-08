"""Unit tests for menu icon DPI size probe (no UNO)."""

from plugin.framework.menu_icon_dpi import interpolate_menu_icon_px, reset_menu_icon_dpi_cache
import pytest


def test_candidate_windows_skips_desktop_create_on_no_vcl():
    from unittest.mock import MagicMock, patch
    from plugin.framework import menu_icon_dpi as m

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    with (
        patch("plugin.framework.appearance.get_style_window", return_value=None),
        patch("plugin.framework.uno_context.desktop_create_is_unsafe", return_value=True),
    ):
        assert m._candidate_windows(ctx) == []
    smgr.createInstanceWithContext.assert_not_called()


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(1.0, 16, id="test_one_x_keeps_16"),
        pytest.param(2.0, 32, id="test_hidpi_prefers_32"),
        pytest.param(1.5, 26, id="test_mid_scale_snaps_to_26"),
        pytest.param(3.0, 32, id="test_ultra_hidpi_caps_at_32"),
    ],
)
def test_one_x_keeps_16(value, expected):
    assert interpolate_menu_icon_px(value) == expected

def test_failed_probe_defaults_to_hidpi_large(monkeypatch):
    from plugin.framework import menu_icon_dpi as m

    reset_menu_icon_dpi_cache()
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_env_scale", lambda: None)
    assert m.resolve_menu_icon_pixel_size() == 32


def test_probe_env_scale_gdk(monkeypatch):
    from plugin.framework import menu_icon_dpi as m

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setenv("GDK_SCALE", "2")
    assert m.probe_env_scale() == 2.0


def test_menu_icon_filename_picks_nearest_shipped(monkeypatch, tmp_path):
    from plugin.framework import menu_icon_dpi as m

    m.reset_menu_icon_dpi_cache()
    # Only 16 + 26 for MCP-style prefixes
    for px in (16, 26):
        (tmp_path / ("running_%s.png" % px)).write_bytes(b"x")

    monkeypatch.setattr(
        "plugin.framework.uno_context.menu_icon_filesystem_paths",
        lambda name: [str(tmp_path / name)],
    )
    assert m.menu_icon_filename("running", px=16) == "running_16.png"
    assert m.menu_icon_filename("running", px=32) == "running_26.png"
    assert m.menu_icon_asset_rel("running", px=16) == "assets/running_16.png"


def test_menu_icon_filename_uses_only_32_when_that_is_all(monkeypatch, tmp_path):
    from plugin.framework import menu_icon_dpi as m

    m.reset_menu_icon_dpi_cache()
    (tmp_path / "python_32.png").write_bytes(b"x")
    monkeypatch.setattr(
        "plugin.framework.uno_context.menu_icon_filesystem_paths",
        lambda name: [str(tmp_path / name)],
    )
    # Even at 1×, only shipped size wins until smaller assets exist.
    assert m.menu_icon_filename("python", px=16) == "python_32.png"


def test_strong_cache_logs_info_once(monkeypatch, caplog):
    import logging
    from plugin.framework import menu_icon_dpi as m
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.info stripped in release bundle")

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 1.0)
    with caplog.at_level(logging.INFO, logger="writeragent.menu_icon_dpi"):
        assert m.resolve_menu_icon_pixel_size() == 16
        assert m.resolve_menu_icon_pixel_size() == 16
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "menu_icon_dpi source=" in r.getMessage()]
    assert len(infos) == 1


def test_non_vcl_probe_stays_weak_until_vcl_dpi(monkeypatch):
    """Font/toolbar/env must not freeze the cache before PixelPerMeterX exists."""
    from plugin.framework import menu_icon_dpi as m

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: 1.0)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_env_scale", lambda: None)
    assert m.resolve_menu_icon_pixel_size() == 16
    assert m._cached_weak is True

    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 2.0)
    assert m.resolve_menu_icon_pixel_size() == 32
    assert m._cached_weak is False
    # A strong vcl reading is not replaced by a later probe.
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 1.0)
    assert m.resolve_menu_icon_pixel_size() == 32


def test_probe_vcl_miss_walks_windows_once(monkeypatch):
    from plugin.framework import menu_icon_dpi as m

    calls = {"n": 0}

    def _windows(ctx=None):
        calls["n"] += 1
        return [object(), object()]

    monkeypatch.setattr(m, "_candidate_windows", _windows)
    monkeypatch.setattr(m, "_ppm_scale", lambda win: None)
    assert m.probe_vcl_dpi_scale() is None
    assert calls["n"] == 1
