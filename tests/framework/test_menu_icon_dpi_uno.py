# WriterAgent - Native UNO Test for menu_icon_dpi
import sys
from unittest.mock import patch

from plugin.framework import menu_icon_dpi as m
from plugin.framework.menu_icon_dpi import (
    _AVAILABLE_PX,
    _DECK_ICONS,
    _DECK_LIST_PATH,
    STOCK_ICON_MATH,
    STOCK_ICON_SEARCH,
    STOCK_ICON_SETTINGS,
    apply_deck_icons,
    load_menu_graphic,
    menu_icon_asset_url,
    reset_menu_icon_dpi_cache,
)
from plugin.framework.uno_context import (
    create_property_value,
    get_service_manager,
    resolve_package_extension_id,
)
from plugin.testing_runner import native_test


@native_test
def test_ladder_pngs_load_at_exact_size(ctx):
    """Every ladder PNG loads through load_menu_graphic with exact SizePixel."""
    reset_menu_icon_dpi_cache()
    for prefix in ("python", "python_cell", "running", "stopped"):
        for px in _AVAILABLE_PX:
            g = load_menu_graphic(ctx, f"{prefix}_{px}.png")
            assert g is not None, f"Failed to load {prefix}_{px}.png"
            assert (g.SizePixel.Width, g.SizePixel.Height) == (px, px)


@native_test
def test_stock_icons_load_with_origin_url(ctx):
    """Each STOCK_ICON_* loads at the device's stock scale and preserves its OriginURL."""
    reset_menu_icon_dpi_cache()
    for url in (STOCK_ICON_SETTINGS, STOCK_ICON_SEARCH, STOCK_ICON_MATH):
        g = load_menu_graphic(ctx, url)
        assert g is not None, f"Failed to load {url}"
        assert g.OriginURL == url
        # ImageRequestParameters::scalePercentage (vcl/source/image/ImplImageTree.cxx:76-84)
        # scales stock icons by the default device DPI percentage (16 at 100%, 32 at 200%).
        w, h = g.SizePixel.Width, g.SizePixel.Height
        assert w == h and w % 16 == 0 and w > 0


@native_test
def test_button_model_keeps_stock_origin_url(ctx):
    """UnoControlButtonModel with stock ImageURL exposes Graphic.OriginURL."""
    smgr = ctx.getServiceManager()
    btn_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlButtonModel", ctx)
    btn_model.ImageURL = menu_icon_asset_url("vnd.sun.star.extension://x", STOCK_ICON_SETTINGS)
    assert btn_model.Graphic is not None
    assert btn_model.Graphic.OriginURL == STOCK_ICON_SETTINGS


@native_test
def test_apply_deck_icons_updates_live_deck_list(ctx):
    """apply_deck_icons stores bucket-sized vnd URLs in the user-layer DeckList."""
    if sys.platform == "darwin":
        return
    smgr = get_service_manager(ctx)
    assert smgr is not None
    prov = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
    ext_id = resolve_package_extension_id(ctx)
    for menu_px, deck_px in ((32, 48), (16, 24)):
        with patch.object(m, "resolve_menu_icon_pixel_size", return_value=menu_px):
            apply_deck_icons(ctx)
        access = prov.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess",
            (create_property_value("nodepath", _DECK_LIST_PATH),),
        )
        found = 0
        for deck, assets in _DECK_ICONS.items():
            if not access.hasByName(deck):
                continue
            found += 1
            expected = f"vnd.sun.star.extension://{ext_id}/assets/{assets[deck_px]}"
            assert access.getByName(deck).getPropertyValue("IconURL") == expected
        assert found > 0
