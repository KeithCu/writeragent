# WriterAgent - Native UNO Test for menu_icon_dpi
from plugin.framework.menu_icon_dpi import (
    _AVAILABLE_PX,
    STOCK_ICON_MATH,
    STOCK_ICON_SEARCH,
    STOCK_ICON_SETTINGS,
    load_menu_graphic,
    menu_icon_asset_url,
    reset_menu_icon_dpi_cache,
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
