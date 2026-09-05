# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot menu-icon pixel-size probe for LibreOffice ImageManager.

Keith's HiDPI Arch already looks good with 16px menu icons (LO scales them).
On 1x boxes those same assets look tiny. Probe VCL DPI (preferred), then
font size, then toolbar/config peers; pick among shipped sizes without
regressing HiDPI.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("writeragent.menu_icon_dpi")

# 96 DPI in device pixels per meter (LO DeviceInfo convention).
_PPM_96DPI = 96.0 / 0.0254  # 3780.0

# Size that already looks good on Keith's HiDPI when LO scales menus.
_HIDPI_KNOWN_GOOD_PX = 16

# Shipped menu-icon suffixes we can select (MCP status has 16 + 26).
_AVAILABLE_PX = (16, 26, 32)

_cached_px: int | None = None
_cached_scale: float | None = None
_cached_weak: bool = False


def reset_menu_icon_dpi_cache() -> None:
    """Test hook: clear the one-shot cache."""
    global _cached_px, _cached_scale, _cached_weak
    _cached_px = None
    _cached_scale = None
    _cached_weak = False


def probe_vcl_dpi_scale(ctx: Any = None) -> float | None:
    """Return VCL device DPI scale vs 96 DPI, or None if unavailable.

    Uses ``XWindow.getInfo().PixelPerMeterX`` (same DeviceInfo LO VCL fills).
    """
    try:
        from plugin.framework.appearance import get_style_window

        win = get_style_window(ctx=ctx)
        if win is None:
            return None
        info = None
        if hasattr(win, "getInfo"):
            info = win.getInfo()
        elif hasattr(win, "Info"):
            info = win.Info
        if info is None:
            return None
        ppm = float(getattr(info, "PixelPerMeterX", 0.0) or 0.0)
        if ppm <= 0.0:
            return None
        return ppm / _PPM_96DPI
    except Exception:
        log.debug("probe_vcl_dpi_scale failed", exc_info=True)
        return None


def probe_menu_font_scale(ctx: Any = None) -> float | None:
    """Fallback: MenuFont/ApplicationFont Height vs AppFont 10."""
    try:
        from plugin.framework.appearance import get_style_window

        win = get_style_window(ctx=ctx)
        if win is None or not hasattr(win, "StyleSettings"):
            return None
        ss = win.StyleSettings
        for attr in ("MenuFont", "ApplicationFont", "ToolFont"):
            fd = getattr(ss, attr, None)
            if fd is None:
                continue
            h = int(getattr(fd, "Height", 0) or 0)
            if h > 0:
                return h / 10.0
    except Exception:
        log.debug("probe_menu_font_scale failed", exc_info=True)
    return None


def probe_toolbar_icon_config_px(ctx: Any = None) -> int | None:
    """Fallback: Office.Common.Misc SidebarIconSize / NotebookbarIconSize.

    LO values: 0=auto; positive enums map toward 16/24/32. Best-effort.
    """
    try:
        import uno
        from com.sun.star.beans import PropertyValue

        from plugin.framework.uno_context import get_service_manager

        if ctx is None:
            ctx = uno.getComponentContext()
        # ty rejects ctx.getServiceManager() on Any; use the shared getattr helper.
        sm = get_service_manager(ctx)
        if sm is None:
            return None
        cfg_prov = sm.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", ctx
        )
        arg = PropertyValue()
        arg.Name = "nodepath"
        arg.Value = "/org.openoffice.Office.Common/Misc"
        access = cfg_prov.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess", (arg,)
        )
        for key in ("SidebarIconSize", "NotebookbarIconSize"):
            try:
                raw = int(access.getByName(key))
            except Exception:
                continue
            if raw <= 0:
                continue
            return {1: 16, 2: 24, 3: 32}.get(raw, 16)
    except Exception:
        log.debug("probe_toolbar_icon_config_px failed", exc_info=True)
    return None


def _nearest_available(px: int) -> int:
    # On a tie, prefer the larger asset (helps 1x; HiDPI still lands on 16).
    return min(_AVAILABLE_PX, key=lambda a: (abs(a - px), -a))


def interpolate_menu_icon_px(scale: float, *, hidpi_good: int = _HIDPI_KNOWN_GOOD_PX) -> int:
    """Map DPI scale to shipped pixel size without blowing up HiDPI.

    At scale~2 (Keith), keep ``hidpi_good`` (16). At scale~1, prefer ~32.
    ``chosen ~= hidpi_good * (2 / scale)``, then snap to available assets.
    """
    if scale <= 0:
        scale = 1.0
    target = hidpi_good * (2.0 / scale)
    target = max(float(_AVAILABLE_PX[0]), min(float(_AVAILABLE_PX[-1]), target))
    return _nearest_available(int(round(target)))


def resolve_menu_icon_pixel_size(ctx: Any = None) -> int:
    """One-shot: probe, interpolate, cache. Safe default leans HiDPI-known-good."""
    global _cached_px, _cached_scale, _cached_weak
    if _cached_px is not None and not _cached_weak:
        return _cached_px

    scale = probe_vcl_dpi_scale(ctx)
    source = "vcl_dpi"
    if scale is None:
        scale = probe_menu_font_scale(ctx)
        source = "font"
    if scale is None:
        cfg_px = probe_toolbar_icon_config_px(ctx)
        if cfg_px is not None:
            scale = (2.0 * _HIDPI_KNOWN_GOOD_PX) / float(cfg_px)
            source = "toolbar_config"
    if scale is None:
        # Prefer Keith's HiDPI-known-good when we cannot probe (startup race).
        _cached_scale = 2.0
        _cached_px = _HIDPI_KNOWN_GOOD_PX
        _cached_weak = True  # retry when a real window exists
        log.info(
            "menu_icon_dpi source=default_hidpi_safe scale=n/a px=%s",
            _cached_px,
        )
        return _cached_px

    px = interpolate_menu_icon_px(float(scale))
    _cached_scale = float(scale)
    _cached_px = px
    _cached_weak = False
    log.info(
        "menu_icon_dpi source=%s scale=%.3f px=%s (hidpi_good=%s)",
        source,
        scale,
        px,
        _HIDPI_KNOWN_GOOD_PX,
    )
    return px


def image_type_for_pixel_size(px: int) -> int:
    """Map pixel size to ``com.sun.star.ui.ImageType`` flags."""
    try:
        from com.sun.star.ui import ImageType

        if px >= 32:
            return int(ImageType.SIZE_32)
        if px >= 24:
            return int(ImageType.SIZE_LARGE)
        return int(ImageType.SIZE_DEFAULT)
    except Exception:
        return 0


def menu_icon_filename(prefix: str, px: int | None = None, ctx: Any = None) -> str:
    """Return ``{prefix}_{px}.png``, falling back to nearest shipped size."""
    if px is None:
        px = resolve_menu_icon_pixel_size(ctx)
    from plugin.framework.uno_context import menu_icon_filesystem_paths

    existing: list[int] = []
    for cand in list(_AVAILABLE_PX) + [16]:
        name = "%s_%s.png" % (prefix, cand)
        if any(os.path.isfile(path) for path in menu_icon_filesystem_paths(name)):
            if cand not in existing:
                existing.append(cand)
    if not existing:
        return "%s_16.png" % prefix
    best = min(existing, key=lambda a: (abs(a - px), -a))
    return "%s_%s.png" % (prefix, best)


__all__ = [
    "reset_menu_icon_dpi_cache",
    "probe_vcl_dpi_scale",
    "probe_menu_font_scale",
    "probe_toolbar_icon_config_px",
    "interpolate_menu_icon_px",
    "resolve_menu_icon_pixel_size",
    "image_type_for_pixel_size",
    "menu_icon_filename",
]
