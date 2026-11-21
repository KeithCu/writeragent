import logging
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from plugin.framework import menu_icon_dpi as m
from plugin.framework.menu_icon_dpi import (
    _AVAILABLE_PX,
    _DECK_ICONS_32,
    STOCK_ICON_SEARCH,
    apply_deck_icons,
    load_menu_graphic,
    menu_icon_filesystem_paths,
    menu_icon_px_for_scale,
    reset_menu_icon_dpi_cache,
)
from tests.harness.strip_bundle import skip_if_release_build


def test_candidate_windows_skips_desktop_create_on_no_vcl():
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
    "dpi, expected",
    [
        pytest.param(96, 16, id="test_96_dpi_16"),
        pytest.param(120, 16, id="test_120_dpi_16"),
        pytest.param(121, 24, id="test_121_dpi_24"),
        pytest.param(168, 24, id="test_168_dpi_24"),
        pytest.param(169, 32, id="test_169_dpi_32"),
        pytest.param(216, 32, id="test_216_dpi_32"),
        pytest.param(217, 40, id="test_217_dpi_40"),
    ],
)
def test_menu_icon_px_matches_libreoffice_dpi_buckets(dpi, expected):
    scale = int(dpi * 1000 / 25.4) / 3780.0
    assert menu_icon_px_for_scale(scale) == expected

def test_failed_probe_defaults_to_hidpi_large(monkeypatch):
    reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_env_scale", lambda: None)
    assert m.resolve_menu_icon_pixel_size() == 32


def test_probe_env_scale_gdk(monkeypatch):
    m.reset_menu_icon_dpi_cache()
    monkeypatch.setenv("GDK_SCALE", "2")
    assert m.probe_env_scale() == 2.0


def test_menu_icon_filename_picks_nearest_shipped(monkeypatch, tmp_path):
    m.reset_menu_icon_dpi_cache()
    # Only 16 + 24 for MCP-style prefixes
    for px in (16, 24):
        (tmp_path / ("running_%s.png" % px)).write_bytes(b"x")

    monkeypatch.setattr(
        "plugin.framework.menu_icon_dpi.menu_icon_filesystem_paths",
        lambda name: [str(tmp_path / name)],
    )
    assert m.menu_icon_filename("running", px=16) == "running_16.png"
    assert m.menu_icon_filename("running", px=32) == "running_24.png"


def test_menu_icon_filename_uses_only_32_when_that_is_all(monkeypatch, tmp_path):
    m.reset_menu_icon_dpi_cache()
    (tmp_path / "python_32.png").write_bytes(b"x")
    monkeypatch.setattr(
        "plugin.framework.menu_icon_dpi.menu_icon_filesystem_paths",
        lambda name: [str(tmp_path / name)],
    )
    # Even at 1×, only shipped size wins until smaller assets exist.
    assert m.menu_icon_filename("python", px=16) == "python_32.png"


def test_menu_icon_filename_caches_and_reset_clears(monkeypatch, tmp_path):
    import os

    reset_menu_icon_dpi_cache()
    (tmp_path / "running_16.png").write_bytes(b"x")
    monkeypatch.setattr(
        "plugin.framework.menu_icon_dpi.menu_icon_filesystem_paths",
        lambda name: [str(tmp_path / name)],
    )

    calls = {"count": 0}
    real_isfile = os.path.isfile

    def count_isfile(path):
        calls["count"] += 1
        return real_isfile(path)

    monkeypatch.setattr("os.path.isfile", count_isfile)

    res1 = m.menu_icon_filename("running", px=16)
    assert res1 == "running_16.png"
    count_first = calls["count"]
    assert count_first > 0

    # Second call uses cache; os.path.isfile not called
    res2 = m.menu_icon_filename("running", px=16)
    assert res2 == "running_16.png"
    assert calls["count"] == count_first

    # After reset, cache is cleared so os.path.isfile is called again
    reset_menu_icon_dpi_cache()
    res3 = m.menu_icon_filename("running", px=16)
    assert res3 == "running_16.png"
    assert calls["count"] > count_first


def test_strong_cache_logs_info_once(monkeypatch, caplog):
    skip_if_release_build("log.info stripped in release bundle")

    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 1.0)
    with caplog.at_level(logging.INFO, logger="writeragent.menu_icon_dpi"):
        assert m.resolve_menu_icon_pixel_size() == 16
        assert m.resolve_menu_icon_pixel_size() == 16
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "menu_icon_dpi source=" in r.getMessage()]
    assert len(infos) == 1


def test_non_vcl_probe_stays_weak_until_vcl_dpi(monkeypatch):
    """Font/toolbar/env must not freeze the cache before PixelPerMeterX exists."""
    m.reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: 1.0)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_env_scale", lambda: None)
    assert m.resolve_menu_icon_pixel_size() == 16
    assert m._state.px is None

    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 2.0)
    assert m.resolve_menu_icon_pixel_size() == 32
    assert m._state.px == 32
    # A strong vcl reading is not replaced by a later probe.
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: 1.0)
    assert m.resolve_menu_icon_pixel_size() == 32


def test_probe_vcl_miss_walks_windows_once(monkeypatch):
    calls = {"n": 0}

    def _windows(ctx=None):
        calls["n"] += 1
        return [object(), object()]

    monkeypatch.setattr(m, "_candidate_windows", _windows)
    monkeypatch.setattr(m, "_ppm_scale", lambda win: None)
    assert m.probe_vcl_dpi_scale() is None
    assert calls["n"] == 1


def test_menu_icon_filesystem_paths_removes_leading_assets_prefix_only():
    import os

    from plugin.framework.menu_icon_dpi import menu_icon_filesystem_paths

    paths1 = menu_icon_filesystem_paths("assets/icon.png")
    assert all(p.endswith(os.path.join("assets", "icon.png")) for p in paths1)

    paths2 = menu_icon_filesystem_paths("/assets/icon.png")
    assert all(p.endswith(os.path.join("assets", "icon.png")) for p in paths2)

    # Substring in directory name must not be stripped
    paths3 = menu_icon_filesystem_paths("my_assets/icon.png")
    assert all(p.endswith(os.path.join("assets", "my_assets", "icon.png")) for p in paths3)


def test_macos_resolves_16_without_probing(monkeypatch):
    reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", "darwin")
    calls = []

    def _record(name, value):
        def _probe(*_args, **_kwargs):
            calls.append(name)
            return value

        return _probe

    monkeypatch.setattr(m, "probe_vcl_dpi_scale", _record("vcl", 2.0))
    monkeypatch.setattr(m, "probe_menu_font_scale", _record("font", 2.0))
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", _record("toolbar", 32))
    monkeypatch.setattr(m, "probe_env_scale", _record("env", 2.0))

    assert m.resolve_menu_icon_pixel_size() == 16
    assert calls == []
    assert m._state.px == 16
    assert m.resolve_menu_icon_pixel_size() == 16


@pytest.mark.parametrize("scale", [1.0, 1.323, 2.0, None])
def test_macos_ignores_every_dpi_scale(monkeypatch, scale):
    reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: scale)
    assert m.resolve_menu_icon_pixel_size() == 16


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize(
    "scale, expected",
    [
        (1.0, 16),
        (1.323, 24),
        (2.0, 32),
        (2.5, 40),
        (None, 32),
    ],
)
def test_non_mac_platforms_keep_dpi_probe(monkeypatch, platform, scale, expected):
    reset_menu_icon_dpi_cache()
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(m, "probe_vcl_dpi_scale", lambda ctx=None: scale)
    monkeypatch.setattr(m, "probe_menu_font_scale", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_toolbar_icon_config_px", lambda ctx=None: None)
    monkeypatch.setattr(m, "probe_env_scale", lambda: None)
    assert m.resolve_menu_icon_pixel_size() == expected


def test_load_menu_graphic_caches_hits_not_misses():
    reset_menu_icon_dpi_cache()
    graphic = object()
    gp = MagicMock()
    gp.queryGraphic.side_effect = [graphic, None, graphic]
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = gp
    ctx = MagicMock()
    ctx.ServiceManager = smgr

    with (
        patch("plugin.framework.menu_icon_dpi.get_extension_url", return_value="vnd.sun.star.extension://test"),
        patch("plugin.framework.menu_icon_dpi.menu_icon_filesystem_paths", return_value=[]),
    ):
        assert load_menu_graphic(ctx, "python_16.png") is graphic
        assert gp.queryGraphic.call_count == 1
        assert load_menu_graphic(ctx, "python_16.png") is graphic
        assert gp.queryGraphic.call_count == 1

        # Misses are not cached; subsequent call retries queryGraphic.
        assert load_menu_graphic(ctx, "missing.png") is None
        assert gp.queryGraphic.call_count == 2
        assert load_menu_graphic(ctx, "missing.png") is graphic
        assert gp.queryGraphic.call_count == 3

        reset_menu_icon_dpi_cache()
        assert gp.queryGraphic.call_count == 3


def test_load_menu_graphic_queries_stock_url_directly():
    reset_menu_icon_dpi_cache()
    graphic = object()
    gp = MagicMock()
    gp.queryGraphic.return_value = graphic
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = gp
    ctx = MagicMock()
    ctx.ServiceManager = smgr

    with (
        patch("plugin.framework.menu_icon_dpi.get_extension_url", return_value="vnd.sun.star.extension://test"),
        patch("os.path.isfile", side_effect=AssertionError("stock icon must not check disk")),
    ):
        result = load_menu_graphic(ctx, STOCK_ICON_SEARCH)

    assert result is graphic
    gp.queryGraphic.assert_called_once()
    pv = gp.queryGraphic.call_args[0][0][0]
    assert pv.Name == "URL"
    assert pv.Value == STOCK_ICON_SEARCH


def test_load_menu_graphic_falls_back_to_disk_when_package_url_throws(tmp_path):
    reset_menu_icon_dpi_cache()
    png = tmp_path / "python_16.png"
    png.write_bytes(b"png")
    graphic = object()
    gp = MagicMock()
    gp.queryGraphic.side_effect = [RuntimeError("vnd not installed"), graphic]
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = gp
    ctx = MagicMock()
    ctx.ServiceManager = smgr

    with (
        patch("plugin.framework.menu_icon_dpi.get_extension_url", return_value="vnd.sun.star.extension://test"),
        patch("plugin.framework.menu_icon_dpi.menu_icon_filesystem_paths", return_value=[str(png)]),
    ):
        result = load_menu_graphic(ctx, "python_16.png")

    assert result is graphic
    assert gp.queryGraphic.call_count == 2


@pytest.mark.parametrize("prefix", ["python", "python_cell", "running", "stopped"])
def test_menu_icon_assets_ship_every_ladder_size(prefix):
    for px in _AVAILABLE_PX:
        paths = [p for p in menu_icon_filesystem_paths(f"{prefix}_{px}.png") if os.path.isfile(p)]
        assert paths, f"Missing asset {prefix}_{px}.png"
        with Image.open(paths[0]) as im:
            assert im.size == (px, px)


class _FakeConfigNode:
    def __init__(self, icon_url: str) -> None:
        self._props = {"IconURL": icon_url}

    def getPropertyValue(self, name: str) -> str:
        return self._props[name]

    def setPropertyValue(self, name: str, value: str) -> None:
        self._props[name] = value


class _FakeDeckListAccess:
    def __init__(self, decks: dict[str, str]) -> None:
        self._nodes = {name: _FakeConfigNode(url) for name, url in decks.items()}
        self.commits = 0
        self.accessed: list[str] = []

    def hasByName(self, name: str) -> bool:
        return name in self._nodes

    def getByName(self, name: str) -> _FakeConfigNode:
        self.accessed.append(name)
        return self._nodes[name]

    def commitChanges(self) -> None:
        self.commits += 1


def _fake_smgr_for_access(access: _FakeDeckListAccess) -> MagicMock:
    prov = MagicMock()
    prov.createInstanceWithArguments.return_value = access
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = prov
    return smgr


def test_apply_deck_icons_skips_macos(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch.object(m, "get_service_manager") as smgr:
        apply_deck_icons(MagicMock())
    smgr.assert_not_called()


def test_apply_deck_icons_writes_32px_urls_once(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    access = _FakeDeckListAccess(
        {
            "WriterAgentDeck": "vnd.sun.star.extension://org.extension.librepy/assets/logo_24.png",
            "LibrePyDeck": "vnd.sun.star.extension://org.extension.librepy/assets/python_24.png",
        }
    )
    smgr = _fake_smgr_for_access(access)
    with (
        patch.object(m, "get_service_manager", return_value=smgr),
        patch.object(m, "resolve_package_extension_id", return_value="org.extension.librepy"),
    ):
        apply_deck_icons(MagicMock())

    assert access.commits == 1
    assert access.getByName("WriterAgentDeck").getPropertyValue("IconURL") == (
        "vnd.sun.star.extension://org.extension.librepy/assets/writeragent_32.png"
    )
    assert access.getByName("LibrePyDeck").getPropertyValue("IconURL") == (
        "vnd.sun.star.extension://org.extension.librepy/assets/python_32.png"
    )


def test_apply_deck_icons_skips_absent_deck_and_unchanged_value(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    access = _FakeDeckListAccess(
        {
            "LibrePyDeck": "vnd.sun.star.extension://org.extension.librepy/assets/python_32.png",
        }
    )
    smgr = _fake_smgr_for_access(access)
    with (
        patch.object(m, "get_service_manager", return_value=smgr),
        patch.object(m, "resolve_package_extension_id", return_value="org.extension.librepy"),
    ):
        apply_deck_icons(MagicMock())

    assert access.commits == 0
    assert "WriterAgentDeck" not in access.accessed


def test_apply_deck_icons_logs_and_survives_config_failure(monkeypatch, caplog):
    monkeypatch.setattr("sys.platform", "linux")
    prov = MagicMock()
    prov.createInstanceWithArguments.side_effect = RuntimeError("config locked")
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = prov
    with (
        patch.object(m, "get_service_manager", return_value=smgr),
        patch.object(m, "resolve_package_extension_id", return_value="org.extension.writeragent"),
        caplog.at_level(logging.ERROR, logger="writeragent.menu_icon_dpi"),
    ):
        apply_deck_icons(MagicMock())

    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("asset", list(_DECK_ICONS_32.values()))
def test_deck_icon_assets_are_32px(asset):
    paths = [p for p in menu_icon_filesystem_paths(asset) if os.path.isfile(p)]
    assert paths, f"Missing asset {asset}"
    with Image.open(paths[0]) as im:
        assert im.size == (32, 32)
