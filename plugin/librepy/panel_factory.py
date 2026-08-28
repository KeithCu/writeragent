# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy Python sidebar panel (Calc + Writer) — UNO factory + XDL shell.

Follows the ChatPanel pattern: XUIElement creates the panel in getRealInterface()
via ContainerWindowProvider + XDL. No chat imports.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, cast

# Minimal stdlib-only bootstrap before any ``plugin.*`` import — unopkg
# writeRegistryInfo loads this file before the OXT root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(3):  # librepy → plugin → OXT root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

import uno
import unohelper

from com.sun.star.container import NoSuchElementException
from com.sun.star.lang import IllegalArgumentException
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement, XUIElementFactory

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=3, also_add_contrib=True)

from plugin.framework.errors import UnoObjectError, suppress_disposed
from plugin.framework.sidebar_column import sidebar_column_width
from plugin.framework.uno_context import get_extension_url, get_ctx

if TYPE_CHECKING:
    from com.sun.star.uno import XInterface

try:
    from com.sun.star.ui.UIElementType import TOOLPANEL  # type: ignore
except ImportError:
    TOOLPANEL = 3

log = logging.getLogger(__name__)

XDL_PATH = "Dialogs/PythonSidebarDialog.xdl"
_PRE_NEGOTIATION_PANEL_WIDTH = 220
_IMPL_NAME = "org.extension.librepy.PythonPanelFactory"


def _get_arg(args, name):
    for pv in args:
        if hasattr(pv, "Name") and pv.Name == name:
            return pv.Value
    return None


def _ensure_paths(ctx) -> None:
    try:
        from plugin.framework.uno_context import get_extension_path

        ext_path = get_extension_path(ctx)
        if ext_path and ext_path not in sys.path:
            sys.path.insert(0, ext_path)
        from plugin.framework.logging import init_logging

        init_logging(ctx)
    except Exception:
        log.debug("LibrePy sidebar path init failed", exc_info=True)


class PythonToolPanel(unohelper.Base, XToolPanel, XSidebarPanel):
    """Holds the panel window; implements XToolPanel and XSidebarPanel."""

    def __init__(self, panel_window, parent_window, ctx):
        self.ctx = ctx
        self.PanelWindow = panel_window
        self.Window = panel_window
        self.parent_window = parent_window
        self.resize_listener = None

    def getWindow(self):
        return self.Window

    def createAccessible(self, ParentAccessible):
        return self.PanelWindow

    def getHeightForWidth(self, nWidth: int):  # pyright: ignore[reportIncompatibleMethodOverride]
        width = nWidth
        if not self.parent_window or not self.PanelWindow or width <= 0:
            return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)
        parent_rect = self.parent_window.getPosSize()
        parent_w = parent_rect.Width
        parent_h = parent_rect.Height
        current_h = 0
        current_w = 0
        with suppress_disposed("getHeightForWidth getPosSize", logger=log):
            before = self.PanelWindow.getPosSize()
            current_h = before.Height if before else 0
            current_w = before.Width if before else 0
        if current_h <= 0:
            current_h = parent_h if parent_h > 0 else 400

        # Fill the content box. min(nWidth, parent); 180 AppFont is a leak.
        # Do not cap at 800px: HiDPI columns are often 900+.
        min_w = self.getMinimalWidth()
        eff_w = sidebar_column_width(width, parent_w, current_w, min_w=min_w)

        log.info(
            "[LIBREPY LAYOUT] getHeightForWidth deck_hint=%s parent=%sx%s eff_w=%s",
            width,
            parent_w,
            parent_h,
            eff_w,
        )
        with suppress_disposed("getHeightForWidth setPosSize", logger=log):
            self.PanelWindow.setPosSize(0, 0, eff_w, current_h, 15)
        rl = getattr(self, "resize_listener", None)
        if rl is not None:
            with suppress_disposed("getHeightForWidth relayout_now", logger=log):
                rl.relayout_now(self.PanelWindow)
        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)

    def getMinimalWidth(self):
        return 220


class PythonPanelElement(unohelper.Base, XUIElement):
    """XUIElement wrapper; creates panel window in getRealInterface() via ContainerWindowProvider."""

    def __init__(self, ctx, frame, parent_window, resource_url):
        self.ctx = ctx
        self.xFrame = frame
        self.xParentWindow = parent_window
        self.ResourceURL = resource_url
        self.Frame = frame
        self.Type = TOOLPANEL
        self.toolpanel = None
        self.m_panelRootWindow = None
        self.controller = None

    def getRealInterface(self) -> XInterface:  # pyright: ignore[reportIncompatibleMethodOverride]
        if not self.toolpanel:
            try:
                _ensure_paths(self.ctx)
                root_window = self._getOrCreatePanelRootWindow()
                self.toolpanel = PythonToolPanel(root_window, self.xParentWindow, self.ctx)
                from plugin.librepy.python_sidebar import PythonSidebarController

                self.controller = PythonSidebarController(self.ctx, root_window, self.xFrame)
                self.toolpanel.resize_listener = getattr(self.controller, "resize_listener", None)
                log.info(
                    "[LIBREPY FIRST LAYOUT] root_w=%d (initial size on app start / sidebar show)",
                    root_window.getPosSize().Width,
                )
            except Exception as e:
                log.exception("PythonPanel getRealInterface failed")
                raise UnoObjectError(
                    "Failed to create LibrePy Python sidebar panel",
                    details={"resource": self.ResourceURL},
                ) from e
        # Panel is a Python UNO component; stubs do not overlap XInterface.
        return cast("XInterface", cast("object", self.toolpanel))

    def _getOrCreatePanelRootWindow(self):
        base_url = get_extension_url(self.ctx)
        dialog_url = base_url + "/" + XDL_PATH
        ctx = self.ctx
        if ctx is None:
            ctx = get_ctx()
        provider = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.ContainerWindowProvider", ctx
        )
        self.m_panelRootWindow = provider.createContainerWindow(dialog_url, "", self.xParentWindow, None)
        if self.m_panelRootWindow and hasattr(self.m_panelRootWindow, "setVisible"):
            with suppress_disposed("setVisible", logger=log):
                self.m_panelRootWindow.setVisible(True)
        with suppress_disposed("constrain panel", logger=log):
            parent_rect = self.xParentWindow.getPosSize()
            target_h = parent_rect.Height if parent_rect.Height > 0 else 400
            self.m_panelRootWindow.setPosSize(0, 0, 220, target_h, 15)
        return self.m_panelRootWindow

    def disposing(self, Source=None):
        try:
            if self.controller is not None:
                self.controller.disposing()
        except Exception:
            pass
        self.controller = None


class PythonPanelFactory(unohelper.Base, XUIElementFactory):
    """Factory that creates PythonPanelElement instances for the LibrePy sidebar."""

    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, ResourceURL, Args):
        resource_url = ResourceURL
        if "PythonPanel" not in resource_url:
            raise NoSuchElementException("Unknown resource: " + resource_url)
        frame = _get_arg(Args, "Frame")
        parent_window = _get_arg(Args, "ParentWindow")
        if not parent_window:
            raise IllegalArgumentException("ParentWindow is required")
        return PythonPanelElement(self.ctx, frame, parent_window, resource_url)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    PythonPanelFactory,
    _IMPL_NAME,
    ("com.sun.star.ui.UIElementFactory",),
)
