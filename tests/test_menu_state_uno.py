# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for MenuIcons.apply writing stock icons to module ImageManagers."""

from __future__ import annotations

from unittest.mock import patch

from plugin.framework.menu_icon_dpi import STOCK_ACTION_ICONS, reset_menu_icon_dpi_cache
from plugin.framework.uno_context import get_ctx, get_service_manager
from plugin.menu_state import MenuIcons
from plugin.testing_runner import native_test

_DOC_MODULES = (
    "com.sun.star.text.TextDocument",
    "com.sun.star.text.WebDocument",
    "com.sun.star.text.GlobalDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.presentation.PresentationDocument",
    "com.sun.star.drawing.DrawingDocument",
)


@native_test
def test_menubar_image_manager_returns_shared_stock_icons(ctx):
    """Module ImageManagers return the stock OriginURL after MenuIcons.apply."""
    reset_menu_icon_dpi_cache()
    with patch("plugin.app.get_menu_icon", return_value=None):
        MenuIcons().apply()

    uno_ctx = get_ctx()
    assert uno_ctx is not None
    smgr = get_service_manager(uno_ctx)
    assert smgr is not None
    supplier = smgr.createInstanceWithContext("com.sun.star.ui.ModuleUIConfigurationManagerSupplier", uno_ctx)

    for mod_id in _DOC_MODULES:
        img_mgr = supplier.getUIConfigurationManager(mod_id).getImageManager()
        for action, url in STOCK_ACTION_ICONS.items():
            cmd = "org.extension.writeragent:" + action
            g = img_mgr.getImages(0, (cmd,))[0]
            assert g is not None, f"{mod_id} {cmd}: got None"
            assert g.OriginURL == url, f"{mod_id} {cmd}: got {g.OriginURL!r}, expected {url!r}"
