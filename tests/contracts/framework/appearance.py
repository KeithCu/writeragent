# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.appearance."""

from __future__ import annotations


import deal

from plugin.framework import appearance as _prod
from tests.contracts.bounds import UNDER_CROSSHAIR

_DEAL_THEME_COLORS = (0, 0xFFFFFF)
_DEAL_DARKEN_FACTORS = (0.0, 1.0) if UNDER_CROSSHAIR else (0.0, 0.5, 0.94, 1.0)


def _deal_theme_color_ok(color: object) -> bool:
    if UNDER_CROSSHAIR:
        return type(color) is int and color in _DEAL_THEME_COLORS
    return type(color) is int and 0 <= color <= 0xFFFFFF


_luminance = deal.post(lambda result: isinstance(result, float) and 0.0 <= result <= 255.0)(
    deal.pre(lambda color: _deal_theme_color_ok(color))(_prod._luminance)
)

_darken = deal.pre(lambda color, factor: _deal_theme_color_ok(color) and factor in _DEAL_DARKEN_FACTORS)(
    _prod._darken
)

get_monaco_theme_info = deal.post(
    lambda result: isinstance(result, dict) and "monaco" in result and "is_dark" in result and "bg" in result
)(_prod.get_monaco_theme_info)

PYTEST = ["_luminance", "_darken", "get_monaco_theme_info"]
TARGETS: list[str] = []
