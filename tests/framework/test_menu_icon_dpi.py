"""Unit tests for menu icon DPI size probe (no UNO)."""

from plugin.framework.menu_icon_dpi import interpolate_menu_icon_px, reset_menu_icon_dpi_cache


def test_one_x_keeps_16():
    assert interpolate_menu_icon_px(1.0) == 16


def test_hidpi_prefers_32():
    assert interpolate_menu_icon_px(2.0) == 32


def test_mid_scale_snaps_to_26():
    assert interpolate_menu_icon_px(1.5) == 26


def test_ultra_hidpi_caps_at_32():
    assert interpolate_menu_icon_px(3.0) == 32


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

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 1.0)
    with caplog.at_level(logging.INFO, logger="writeragent.menu_icon_dpi"):
        assert m.resolve_menu_icon_pixel_size() == 16
        assert m.resolve_menu_icon_pixel_size() == 16
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "menu_icon_dpi source=" in r.getMessage()]
    assert len(infos) == 1
