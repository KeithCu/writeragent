# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot menu-icon pixel-size probe for LibreOffice ImageManager.

Pick shipped menu-icon pixel sizes from display scale.

HiDPI (~2×) wants the large assets (26/32). Little/1× screens want 16.
Probes in order: VCL DPI, font, toolbar config, then env scale.
Only VCL DPI is a strong reading; other probes remain weak until VCL DPI is available.
Probe miss defaults to 32 px.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from plugin.framework.constants import get_plugin_dir

log = logging.getLogger("writeragent.menu_icon_dpi")

# 96 DPI in device pixels per meter (LO DeviceInfo convention).
_PPM_96DPI = 96.0 / 0.0254  # 3780.0

# Base 1× asset size; HiDPI prefers the largest shipped asset.
_ONE_X_PX = 16
_HIDPI_PX = 32

# Shipped menu-icon suffixes we can select (MCP status has 16 + 26).
_AVAILABLE_PX = (16, 26, 32)


class _State:
    px: int | None
    scale: float | None
    weak: bool
    logged_strong: bool
    filename_cache: dict[tuple[str, int], str]

    def __init__(self) -> None:
        self.px = None
        self.scale = None
        self.weak = False
        self.logged_strong = False
        self.filename_cache = {}


_state = _State()


def reset_menu_icon_dpi_cache() -> None:
    """Test hook: clear the one-shot cache."""
    _state.px = None
    _state.scale = None
    _state.weak = False
    _state.logged_strong = False
    _state.filename_cache = {}


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
    # Dedupe by id while preserving order.
    out: list[Any] = []
    seen: set[int] = set()
    for w in wins:
        wid = id(w)
        if wid in seen:
            continue
        seen.add(wid)
        out.append(w)
    return out


def probe_vcl_dpi_scale(ctx: Any = None) -> float | None:
    """Return VCL device DPI scale vs 96 DPI, or None if unavailable.

    Uses ``XWindow.getInfo().PixelPerMeterX`` (same DeviceInfo LO VCL fills).
    Tries StyleSettings host plus active/open frame windows — KDE/Wayland
    often has no usable style window at menu-icon time.
    """
    try:
        # The debug line used to call _candidate_windows again, so a total
        # miss built the desktop/frame list twice.
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


def _nearest_available(px: int) -> int:
    # On a tie, prefer the larger asset (HiDPI gets 26/32).
    return min(_AVAILABLE_PX, key=lambda a: (abs(a - px), -a))


def interpolate_menu_icon_px(scale: float, *, one_x: int = _ONE_X_PX) -> int:
    """Map DPI scale to shipped pixel size.

    At scale~1 → 16. At scale~2 → 32 (HiDPI highres). Mid scales snap to 26.
    ``chosen ~= one_x * scale``, then snap to available assets.
    """
    if scale <= 0:
        scale = 1.0
    target = one_x * scale
    target = max(float(_AVAILABLE_PX[0]), min(float(_AVAILABLE_PX[-1]), target))
    return _nearest_available(int(round(target)))


def _toolbar_scale(ctx: Any) -> float | None:
    cfg_px = probe_toolbar_icon_config_px(ctx)
    if cfg_px is None:
        return None
    return float(cfg_px) / float(_ONE_X_PX)


def resolve_menu_icon_pixel_size(ctx: Any = None) -> int:
    """One-shot: probe, interpolate, cache. Probe-miss prefers HiDPI large assets."""
    if _state.px is not None and not _state.weak:
        return _state.px

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
        _state.scale = 2.0
        _state.px = _HIDPI_PX
        _state.weak = True  # retry when a real window exists
        log.debug("menu_icon_dpi source=default_hidpi_large scale=n/a px=%s (weak; will retry)", _state.px)
        return _state.px

    px = interpolate_menu_icon_px(float(scale))
    _state.scale = float(scale)
    _state.px = px
    # Only vcl_dpi (PixelPerMeterX) is a strong reading. Font, toolbar config,
    # and env used to set _cached_weak False, so a startup call before a real
    # window existed never upgraded when a later window reported ~2x.
    _state.weak = source != "vcl_dpi"
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


def menu_icon_asset_url(ext_url: str, icon_filename: str) -> str:
    """Return GraphicProvider URL for a menu icon shipped in OXT assets/."""
    return "%s/assets/%s" % (ext_url.rstrip("/"), icon_filename)


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


def menu_icon_asset_rel(prefix: str, px: int | None = None, ctx: Any = None) -> str:
    """Return ``assets/{prefix}_{px}.png`` for button ``ImageURL`` paths."""
    return "assets/" + menu_icon_filename(prefix, px, ctx)


__all__ = [
    "reset_menu_icon_dpi_cache",
    "probe_vcl_dpi_scale",
    "probe_menu_font_scale",
    "probe_toolbar_icon_config_px",
    "interpolate_menu_icon_px",
    "resolve_menu_icon_pixel_size",
    "image_type_for_pixel_size",
    "menu_icon_filename",
    "menu_icon_asset_rel",
    "menu_icon_asset_url",
    "menu_icon_filesystem_paths",
    "pick_shipped_icon",
]
