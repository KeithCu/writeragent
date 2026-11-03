# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified AutoLayout table and resolver for Draw and Impress slides.

Maps friendly names (e.g. ``text``, ``blank``), aliases (e.g. ``none``),
and Collabora/xmloff constants (e.g. ``AUTOLAYOUT_TITLE_CONTENT``) to
LibreOffice AutoLayout integer ids (include/xmloff/autolayout.hxx).
"""

from __future__ import annotations

import math
from typing import Any

LAYOUT_TABLE: tuple[tuple[str | None, str, int, tuple[str, ...], str], ...] = (
    ("title", "AUTOLAYOUT_TITLE", 0, (), "title + subtitle"),
    ("text", "AUTOLAYOUT_TITLE_CONTENT", 1, (), "title + one content area"),
    ("chart", "AUTOLAYOUT_CHART", 2, (), ""),
    ("two_column_text", "AUTOLAYOUT_TITLE_2CONTENT", 3, ("two_objects",), "title + two content areas side by side"),
    ("text_and_chart", "AUTOLAYOUT_TEXTCHART", 4, (), ""),
    ("org_chart", "AUTOLAYOUT_ORG", 5, (), ""),
    ("text_and_clipart", "AUTOLAYOUT_TEXTCLIP", 6, (), ""),
    ("chart_and_text", "AUTOLAYOUT_CHARTTEXT", 7, (), ""),
    ("table", "AUTOLAYOUT_TAB", 8, (), ""),
    ("clipart_and_text", "AUTOLAYOUT_CLIPTEXT", 9, (), ""),
    ("text_and_object", "AUTOLAYOUT_TEXTOBJ", 10, (), ""),
    ("object", "AUTOLAYOUT_OBJ", 11, ("large_object",), ""),
    ("two_column_and_object", "AUTOLAYOUT_TITLE_CONTENT_2CONTENT", 12, ("object_and_two_objects",), ""),
    ("object_and_text", "AUTOLAYOUT_OBJTEXT", 13, (), ""),
    ("object_over_text", "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT", 14, (), ""),
    ("object_and_two_column", "AUTOLAYOUT_TITLE_2CONTENT_CONTENT", 15, ("two_objects_and_object",), ""),
    ("two_objects_over_text", "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT", 16, (), ""),
    ("text_over_object", "AUTOLAYOUT_TEXTOVEROBJ", 17, (), ""),
    ("four_objects", "AUTOLAYOUT_TITLE_4CONTENT", 18, (), ""),
    ("title_only", "AUTOLAYOUT_TITLE_ONLY", 19, (), ""),
    ("blank", "AUTOLAYOUT_NONE", 20, ("none",), ""),
    (None, "AUTOLAYOUT_NOTES", 21, (), ""),
    (None, "AUTOLAYOUT_HANDOUT1", 22, (), ""),
    (None, "AUTOLAYOUT_HANDOUT2", 23, (), ""),
    (None, "AUTOLAYOUT_HANDOUT3", 24, (), ""),
    (None, "AUTOLAYOUT_HANDOUT4", 25, (), ""),
    (None, "AUTOLAYOUT_HANDOUT6", 26, (), ""),
    ("vertical_title_and_text_over_chart", "AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT", 27, (), ""),
    ("vertical_title_and_text", "AUTOLAYOUT_VTITLE_VCONTENT", 28, (), ""),
    ("vertical_text", "AUTOLAYOUT_TITLE_VCONTENT", 29, (), ""),
    ("title_two_vertical_text", "AUTOLAYOUT_TITLE_2VTEXT", 30, (), ""),
    (None, "AUTOLAYOUT_HANDOUT9", 31, (), ""),
    ("only_text", "AUTOLAYOUT_ONLY_TEXT", 32, ("centered_text",), ""),
    (None, "AUTOLAYOUT_4CLIPART", 33, (), ""),
    ("six_objects", "AUTOLAYOUT_TITLE_6CONTENT", 34, (), ""),
)

_NAME_TO_ID: dict[str, int] = {}
_ID_TO_NAME: dict[int, str] = {}
_AVAILABLE_NAMES: list[str] = []

for _friendly, _auto, _id, _aliases, _blurb in LAYOUT_TABLE:
    _NAME_TO_ID[_auto.lower()] = _id
    if _friendly is not None:
        _NAME_TO_ID[_friendly.lower()] = _id
        _ID_TO_NAME[_id] = _friendly
        _AVAILABLE_NAMES.append(_friendly)
    for _alias in _aliases:
        _NAME_TO_ID[_alias.lower()] = _id
        _AVAILABLE_NAMES.append(_alias)

_AVAILABLE_NAMES.sort()
VALID_LAYOUT_IDS: frozenset[int] = frozenset(row[2] for row in LAYOUT_TABLE)


def layout_id(name_or_id: Any) -> int | None:
    """Return the LibreOffice AutoLayout integer id for a friendly name, alias, AUTOLAYOUT_* name, or id."""
    if isinstance(name_or_id, bool) or name_or_id is None:
        return None
    if isinstance(name_or_id, int):
        return name_or_id
    if isinstance(name_or_id, float):
        if not math.isnan(name_or_id) and name_or_id.is_integer():
            return int(name_or_id)
        return None
    if isinstance(name_or_id, str):
        key = name_or_id.strip()
        if not key:
            return None
        lower = key.lower()
        if lower in _NAME_TO_ID:
            return _NAME_TO_ID[lower]
        try:
            return int(key)
        except ValueError:
            return None
    return None


def layout_name(id: int) -> str:
    """Return the canonical friendly name for an AutoLayout id, or 'unknown_<id>'."""
    return _ID_TO_NAME.get(id, f"unknown_{id}")


def available_layout_names() -> list[str]:
    """Canonical friendly names plus aliases accepted by slide layout tools."""
    return list(_AVAILABLE_NAMES)
