# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared LibreOffice theme / appearance detection.

Central source for light/dark decisions and colors derived from VCL StyleSettings
on a window (FieldColor, DialogColor etc.). Used by chat sidebar and the Monaco
Python editor (so the webview chrome + Monaco theme track the LO UI automatically).

Detection is heuristic (luminance of FieldColor) because LO exposes limited
color tokens via UNO and "System" mode reflects the desktop at the time the
window was created. This matches the logic previously only in rich_text.py.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.deal_shim import UNDER_CROSSHAIR, deal
from plugin.framework.uno_context import get_desktop

log = logging.getLogger(__name__)

# Safe light fallback (matches prior defaults)
_FALLBACK_BG = 0xE0E1E2
_FALLBACK_USER = 0x2A6099
_FALLBACK_ASSISTANT = 0x1E293B
_DARK_USER = 0x60A5FA
_DARK_ASSISTANT = 0xE2E8F0


def _container_window(frame: Any) -> Any | None:
    if frame is None:
        return None
    return getattr(frame, "getContainerWindow", lambda: None)()


def _window_from_doc(doc: Any) -> Any | None:
    if UNDER_CROSSHAIR or doc is None:
        return None
    try:
        controller = doc.getCurrentController()
        if controller:
            return _container_window(controller.getFrame())
    except Exception as e:
        log.debug("get_style_window: doc frame lookup failed: %s", e)
    return None


def _window_from_desktop(ctx: Any) -> Any | None:
    if UNDER_CROSSHAIR or ctx is None:
        return None
    try:
        desktop = get_desktop(ctx)
        win = _container_window(desktop.getCurrentFrame())
        if win is None:
            comp = desktop.getCurrentComponent()
            if comp is not None and hasattr(comp, "getCurrentController"):
                ctrl = comp.getCurrentController()
                if ctrl:
                    win = _container_window(getattr(ctrl, "getFrame", lambda: None)())
        return win
    except Exception as e:
        log.debug("get_style_window: ctx/desktop lookup failed: %s", e)
    return None


def get_style_window(doc: Any = None, style_window: Any = None, ctx: Any = None) -> Any | None:
    """Return a VCL window likely to expose StyleSettings, or None.

    Preference order: explicit style_window, doc's frame container, current
    desktop frame's container (for cases with no doc like Run Python Script).
    """
    if style_window is not None:
        return style_window
    return _window_from_doc(doc) or _window_from_desktop(ctx)


# CrossHair: two RGB samples. Pytest keeps 0.94 — light _resolve_style uses it.
_DEAL_THEME_COLORS = (0, 0xFFFFFF)
_DEAL_DARKEN_FACTORS = (0.0, 1.0) if UNDER_CROSSHAIR else (0.0, 0.5, 0.94, 1.0)


def _deal_theme_color_ok(color: object) -> bool:
    if UNDER_CROSSHAIR:
        return type(color) is int and color in _DEAL_THEME_COLORS
    return type(color) is int and 0 <= color <= 0xFFFFFF


@deal.pre(lambda color: _deal_theme_color_ok(color))
@deal.post(lambda result: isinstance(result, float) and 0.0 <= result <= 255.0)
def _luminance(color: int) -> float:
    # crosshair: off
    # ``type(color) is int`` rejects bool; 24-bit RGB.
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@deal.pre(lambda color, factor: _deal_theme_color_ok(color) and factor in _DEAL_DARKEN_FACTORS)
def _darken(color: int, factor: float) -> int:
    # crosshair: off
    r = int(((color >> 16) & 0xFF) * factor)
    g = int(((color >> 8) & 0xFF) * factor)
    b = int((color & 0xFF) * factor)
    return (r << 16) | (g << 8) | b


def _resolve_style(win: Any) -> tuple[bool, int]:
    """Return (is_dark, bg) from VCL StyleSettings, or fallback light gray."""
    if UNDER_CROSSHAIR or win is None or not hasattr(win, "StyleSettings"):
        return False, _FALLBACK_BG
    style_settings = win.StyleSettings
    if not style_settings:
        return False, _FALLBACK_BG
    field_color = getattr(style_settings, "FieldColor", 0xFFFFFF)
    if not isinstance(field_color, int):
        return False, _FALLBACK_BG
    if _luminance(field_color) < 128:
        return True, field_color
    dialog_color = getattr(style_settings, "DialogColor", 0xEFF0F1)
    if isinstance(dialog_color, int):
        return False, _darken(dialog_color, 0.94)
    return False, _FALLBACK_BG


def _resolve_theme(doc: Any = None, style_window: Any = None, ctx: Any = None, *, label: str = "") -> tuple[bool, int]:
    win = get_style_window(doc=doc, style_window=style_window, ctx=ctx)
    try:
        return _resolve_style(win)
    except Exception as e:
        log.debug("%s: %s", label, e)
        return False, _FALLBACK_BG


def get_theme_colors(doc: Any = None, style_window: Any = None, ctx: Any = None) -> tuple[int, int, int]:
    """Retrieve theme-aware colors based on StyleSettings.

    Returns (bg_color, user_color, assistant_color) as 0xRRGGBB ints.
    Delegates to the same logic used for the Monaco editor's is_dark decision.
    Safe fallback is a soft light gray.
    """
    is_dark, bg = _resolve_theme(
        doc=doc,
        style_window=style_window,
        ctx=ctx,
        label="Failed to resolve theme colors from StyleSettings",
    )
    if is_dark:
        return bg, _DARK_USER, _DARK_ASSISTANT
    return bg, _FALLBACK_USER, _FALLBACK_ASSISTANT


@deal.post(lambda result: isinstance(result, dict) and "monaco" in result and "is_dark" in result and "bg" in result)
def get_monaco_theme_info(doc: Any = None, style_window: Any = None, ctx: Any = None) -> dict[str, Any]:
    """Return theme descriptor for the Python Monaco editor.

    The dict is sent over the IPC load (or a later 'theme' update) message.
    JS uses "monaco" for monaco.editor.setTheme and "is_dark" to toggle
    CSS classes on the toolbar chrome so the whole pywebview matches LO.

    We keep the decision in one place with the chat sidebar.
    Richer palette (hex colors etc.) can be added later without protocol break.
    """
    # UNO StyleSettings host; Any args have no finite domain.
    # crosshair: off
    is_dark, bg = _resolve_theme(
        doc=doc,
        style_window=style_window,
        ctx=ctx,
        label="get_monaco_theme_info failed",
    )
    return {"monaco": "vs-dark" if is_dark else "vs", "is_dark": is_dark, "bg": bg}


__all__ = ["get_style_window", "get_theme_colors", "get_monaco_theme_info"]
