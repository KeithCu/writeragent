# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot menu-icon pixel-size probe and graphic loader for LibreOffice.

Pick shipped menu-icon pixel sizes (16/24/32/40) from display scale using
LibreOffice's DPI buckets. Generic actions use LibreOffice stock icons
(``private:graphicrepository/...``); brand marks stay PNG.

Probes in order: VCL DPI, font, toolbar config, then env scale.
Only VCL DPI is a strong reading; other probes remain weak until VCL DPI is available.
Probe miss defaults to 32 px.
macOS is fixed at 16: VCL pixels are points there and CoreGraphics applies the Retina backing scale itself.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import uno

from plugin.framework.constants import get_plugin_dir
from plugin.framework.uno_context import create_property_value, get_extension_url, get_service_manager

log = logging.getLogger("writeragent.menu_icon_dpi")

# 96 DPI in device pixels per meter (LO DeviceInfo convention).
_PPM_96DPI = 96.0 / 0.0254  # 3780.0

# Base 1× asset size; HiDPI probe miss defaults to 32 px.
_ONE_X_PX = 16
_HIDPI_PX = 32

# Stock LibreOffice icons are kept as stock images by Image::Image(XGraphic)
# (vcl/source/image/Image.cxx) and reloaded at the drawing HiDPI scale and
# active dark/light icon theme by ImplImage::getBitmapForHiDPI
# (vcl/source/image/ImplImage.cxx). Brand marks stay PNG.
_STOCK_URL_PREFIX = "private:graphicrepository/"
STOCK_ICON_SETTINGS = _STOCK_URL_PREFIX + "cmd/sc_optionstreedialog.png"
STOCK_ICON_SEARCH = _STOCK_URL_PREFIX + "cmd/sc_recsearch.png"
STOCK_ICON_MATH = _STOCK_URL_PREFIX + "cmd/sc_insertmath.png"

# Shipped menu-icon suffixes we can select.
_AVAILABLE_PX = (16, 24, 32, 40)
# LibreOffice CountDPIScaleFactor thresholds: DPI > n picks that bucket's size.
_DPI_BUCKETS = ((216, 40), (168, 32), (120, 24))


class _State:
    px: int | None
    logged_strong: bool
    filename_cache: dict[tuple[str, int], str]
    graphic_cache: dict[tuple[str, str], Any]

    def __init__(self) -> None:
        self.px = None
        self.logged_strong = False
        self.filename_cache = {}
        self.graphic_cache = {}


_state = _State()


def reset_menu_icon_dpi_cache() -> None:
    """Test hook: clear the one-shot cache."""
    _state.px = None
    _state.logged_strong = False
    _state.filename_cache = {}
    _state.graphic_cache = {}


def _ppm_scale(win: Any) -> float | None:
    if win is None:
        return None
    info = None
    try:
        if hasattr(win, "getInfo"):
            info = win.getInfo()
        elif hasattr(win, "Info"):
            info = win.Info
    except Exception:
        return None
    if info is None:
        return None
    ppm = float(getattr(info, "PixelPerMeterX", 0.0) or 0.0)
    if ppm <= 0.0:
        return None
    return ppm / _PPM_96DPI


def _candidate_windows(ctx: Any = None) -> list[Any]:
    """StyleSettings host first, then desktop/frame peers (Wayland/KDE often need these)."""
    wins: list[Any] = []
    from plugin.framework.appearance import get_style_window

    w = get_style_window(ctx=ctx)
    if w is not None:
        wins.append(w)
    try:
        from plugin.framework.uno_context import desktop_create_is_unsafe, get_ctx, get_desktop

        if desktop_create_is_unsafe():
            return wins
        if ctx is None:
            ctx = get_ctx()
        # get_desktop refuses to create Desktop when there is no VCL (#768).
        desktop = get_desktop(ctx)
        if desktop is None:
            return wins
        frames_to_try: list[Any] = []
        try:
            active = desktop.getActiveFrame()
            if active is not None:
                frames_to_try.append(active)
        except Exception:
            log.debug("menu icon DPI: getActiveFrame failed", exc_info=True)
        try:
            frames = desktop.getFrames()
            for i in range(int(frames.getCount())):
                frames_to_try.append(frames.getByIndex(i))
        except Exception:
            log.debug("menu icon DPI: getFrames failed", exc_info=True)
        for frame in frames_to_try:
            if frame is None:
                continue
            for name in ("getContainerWindow", "getComponentWindow"):
                fn = getattr(frame, name, None)
                if not callable(fn):
                    continue
                try:
                    w = fn()
                    if w is not None:
                        wins.append(w)
                except Exception:
                    log.debug("menu icon DPI: %s failed", name, exc_info=True)
    except Exception:
        log.debug("candidate windows failed", exc_info=True)
    # Dedupe by id while preserving order (first wins).
    return list({id(w): w for w in wins}.values())


def probe_vcl_dpi_scale(ctx: Any = None) -> float | None:
    """Return VCL device DPI scale vs 96 DPI, or None if unavailable.

    Uses ``XWindow.getInfo().PixelPerMeterX`` (same DeviceInfo LO VCL fills).
    Tries StyleSettings host plus active/open frame windows — KDE/Wayland
    often has no usable style window at menu-icon time.
    """
    try:
        windows = _candidate_windows(ctx)
        for win in windows:
            scale = _ppm_scale(win)
            if scale is not None:
                return scale
        log.debug("menu_icon_dpi probe_vcl_dpi: no PixelPerMeterX on %s windows", len(windows))
        return None
    except Exception:
        log.debug("probe_vcl_dpi_scale failed", exc_info=True)
        return None


def probe_env_scale() -> float | None:
    """Last-resort desktop scale from GDK_SCALE / QT_SCALE_FACTOR (Arch HiDPI often sets these)."""
    for key in ("GDK_SCALE", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS"):
        raw = os.environ.get(key)
        if not raw:
            continue
        # QT_SCREEN_SCALE_FACTORS can be "2;2" or "HDMI-1=2"
        token = raw.replace(";", "=").split("=")[-1].split(";")[0].strip()
        try:
            val = float(token)
        except ValueError:
            continue
        if val > 0:
            return val
    return None


def probe_menu_font_scale(ctx: Any = None) -> float | None:
    """Fallback: MenuFont/ApplicationFont Height vs AppFont 10."""
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
    return None


def probe_toolbar_icon_config_px(ctx: Any = None) -> int | None:
    """Fallback: Office.Common.Misc SidebarIconSize / NotebookbarIconSize.

    LO values: 0=auto; positive enums map toward 16/24/32. Best-effort.
    """
    try:
        from com.sun.star.beans import PropertyValue

        from plugin.framework.uno_context import get_ctx, get_service_manager

        if ctx is None:
            ctx = get_ctx()
        # ty rejects ctx.getServiceManager() on Any; use the shared getattr helper.
        sm = get_service_manager(ctx)
        if sm is None:
            return None
        cfg_prov = sm.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
        arg = PropertyValue()
        arg.Name = "nodepath"
        arg.Value = "/org.openoffice.Office.Common/Misc"
        access = cfg_prov.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (arg,))
        for key in ("SidebarIconSize", "NotebookbarIconSize"):
            try:
                raw = int(access.getByName(key))
            except Exception:
                log.debug("menu icon DPI: config key %s unreadable", key, exc_info=True)
                continue
            if raw <= 0:
                continue
            return {1: 16, 2: 24, 3: 32}.get(raw, 16)
    except Exception:
        log.debug("probe_toolbar_icon_config_px failed", exc_info=True)
    return None


def menu_icon_px_for_scale(scale: float) -> int:
    """Map DPI scale to shipped pixel size using LibreOffice's DPI buckets.

    Matches ``CountDPIScaleFactor`` (vcl/source/window/window.cxx):
    DPI > 216 → 40 (250%), > 168 → 32 (200%), > 120 → 24 (150%), else 16 (100%).
    Rounding is needed because ``PixelPerMeterX`` is integer-truncated
    (vcl/source/outdev/outdev.cxx ``GetCommonDeviceInfo``).
    """
    if scale <= 0:
        return _ONE_X_PX
    dpi = round(scale * 96)
    for threshold, px in _DPI_BUCKETS:
        if dpi > threshold:
            return px
    return _ONE_X_PX


def _toolbar_scale(ctx: Any) -> float | None:
    cfg_px = probe_toolbar_icon_config_px(ctx)
    if cfg_px is None:
        return None
    return float(cfg_px) / float(_ONE_X_PX)


def resolve_menu_icon_pixel_size(ctx: Any = None) -> int:
    """One-shot: return 16 on macOS, else probe, bucket, and cache (miss prefers HiDPI)."""
    if _state.px is not None:
        return _state.px

    if sys.platform == "darwin":
        # VCL never applies a HiDPI scale on macOS (vcl/source/window/window.cxx
        # CountDPIScaleFactor returns 100 under MACOSX); its pixels are Cocoa points
        # and CoreGraphics doubles them on Retina. A PNG loaded by URL is drawn at
        # its bitmap size in points (vcl/source/image/ImplImage.cxx ImplImage(Bitmap)),
        # so the 1x assets are the right size on every Mac display, and the DPI
        # probe (vcl/quartz/salgdicommon.cxx initResolution reports the clamped
        # logical DPI, never the backing scale) must not run.
        _state.px = _ONE_X_PX
        if not _state.logged_strong:
            log.info("menu_icon_dpi source=macos px=%s", _ONE_X_PX)
            _state.logged_strong = True
        return _ONE_X_PX

    probes = (
        ("vcl_dpi", probe_vcl_dpi_scale),
        ("font", probe_menu_font_scale),
        ("toolbar_config", _toolbar_scale),
        ("env_scale", lambda _c: probe_env_scale()),
    )

    scale = None
    source = None
    for src, probe in probes:
        scale = probe(ctx)
        if scale is not None:
            source = src
            break

    if scale is None or source is None:
        # Probe miss is common on Wayland/KDE at startup — assume HiDPI wants big assets.
        log.debug("menu_icon_dpi source=default_hidpi_large scale=n/a px=%s (weak; will retry)", _HIDPI_PX)
        return _HIDPI_PX

    px = menu_icon_px_for_scale(float(scale))
    # Only PixelPerMeterX is cached. Font, toolbar, and env stay uncached and retry.
    if source == "vcl_dpi":
        _state.px = px
    if not _state.logged_strong:
        log.info("menu_icon_dpi source=%s scale=%.3f px=%s", source, scale, px)
        _state.logged_strong = True
    else:
        log.debug("menu_icon_dpi source=%s scale=%.3f px=%s (cache refresh)", source, scale, px)
    return px


def image_type_for_pixel_size(px: int) -> int:
    """Map pixel size to ``com.sun.star.ui.ImageType`` flags."""
    from com.sun.star.ui import ImageType

    if px >= 32:
        return int(ImageType.SIZE_32)
    if px >= 24:
        return int(ImageType.SIZE_LARGE)
    return int(ImageType.SIZE_DEFAULT)


def menu_icon_asset_url(ext_url: str, icon: str) -> str:
    """Return GraphicProvider/ImageURL for *icon* (stock URL as is, else OXT assets/)."""
    return icon if icon.startswith(_STOCK_URL_PREFIX) else "%s/assets/%s" % (ext_url.rstrip("/"), icon)


def menu_icon_filesystem_paths(icon_filename: str) -> tuple[str, ...]:
    """Local PNG paths for menu icons (OXT layout first, then git checkout).

    ``scripts/build_oxt.py`` remaps ``extension/assets/`` to ``assets/`` at the
    bundle root. ``make release`` pytest/UNO runs against that tree, so looking
    only under ``extension/assets/`` misses ``python_32.png`` and friends.

    ``removeprefix("assets/")`` after ``lstrip("/")`` removes only a leading
    assets/ prefix. ``replace("assets/", "")`` strips that substring
    anywhere ("my_assets/x.png" becomes "my_x.png").
    On Windows, ``os.path.join`` does not rewrite a "/" already inside the
    next component, so "my_assets/icon.png" becomes
    assets\\my_assets/icon.png and fails the endswith check
    (GHA 37719557033). ``normpath`` the relative remainder so separators
    match the platform.
    """
    clean = icon_filename.replace("\\", "/").lstrip("/").removeprefix("assets/")
    # normpath("") is "."; an empty remainder must stay empty so the join
    # remains under assets/ rather than collapsing to the assets directory.
    if clean:
        clean = os.path.normpath(clean)
    root = os.path.dirname(get_plugin_dir())
    return (os.path.join(root, "assets", clean), os.path.join(root, "extension", "assets", clean))


def pick_shipped_icon(prefix: str, wanted_px: int, candidates: tuple[int, ...], fallback_px: int) -> str:
    """Pick closest existing icon matching *wanted_px* among *candidates*, or fallback."""
    existing: list[int] = []
    for cand in candidates:
        name = f"{prefix}_{cand}.png"
        if any(os.path.isfile(path) for path in menu_icon_filesystem_paths(name)):
            existing.append(cand)
    if not existing:
        return f"{prefix}_{fallback_px}.png"
    best = min(existing, key=lambda a: (abs(a - wanted_px), -a))
    return f"{prefix}_{best}.png"


def menu_icon_filename(prefix: str, px: int | None = None, ctx: Any = None) -> str:
    """Return ``{prefix}_{px}.png``, falling back to nearest shipped size."""
    if px is None:
        px = resolve_menu_icon_pixel_size(ctx)
    cache_key = (prefix, px)
    cached = _state.filename_cache.get(cache_key)
    if cached is not None:
        return cached

    res = pick_shipped_icon(prefix, px, _AVAILABLE_PX, fallback_px=16)
    _state.filename_cache[cache_key] = res
    return res


def load_menu_graphic(ctx: Any, icon: str) -> Any:
    """Load a stock URL or extension asset PNG as XGraphic.

    Stock URLs (``private:graphicrepository/...``) go straight to ``queryGraphic``.
    Asset filenames try the ``vnd.sun.star.extension://`` URL first, then local
    disk paths; the vnd URL throws when the OXT is not installed (e.g.
    ``testing_runner``'s fresh profile), so disk fallback keeps tests working.
    """
    try:
        ext_url = get_extension_url(ctx) or ""
        cache_key = (ext_url, icon)
        cached = _state.graphic_cache.get(cache_key)
        if cached is not None:
            return cached

        if icon.startswith(_STOCK_URL_PREFIX):
            urls = [icon]
        else:
            urls = [menu_icon_asset_url(ext_url, icon)] if ext_url else []
            for path in menu_icon_filesystem_paths(icon):
                if os.path.isfile(path):
                    urls.append(uno.systemPathToFileUrl(path))

        smgr = get_service_manager(ctx)
        if smgr is None:
            return None
        gp = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        for url in urls:
            try:
                graphic = gp.queryGraphic((create_property_value("URL", url),))
                if graphic is not None:
                    _state.graphic_cache[cache_key] = graphic
                    return graphic
            except Exception:
                log.debug("load_menu_graphic URL failed for %s (%s)", icon, url, exc_info=True)
        return None
    except Exception:
        log.debug("load_menu_graphic failed for %s", icon, exc_info=True)
        return None
