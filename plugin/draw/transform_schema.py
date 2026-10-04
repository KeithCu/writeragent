# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Canonical transform JSON DSL (Collabora Online — keep in sync when extending):
#   https://github.com/CollaboraOnline/online/blob/master/wsd/DocumentToolDescriptions.hpp
# Kit applies via .uno:TransformDocumentStructure (LOKit only; WriterAgent uses PyUNO).
"""Schema helpers for transform_document_structure (Collabora-compatible JSON)."""

from __future__ import annotations

from typing import Any

from plugin.framework.json_utils import safe_json_loads

# Upstream source for TRANSFORM_PARAM_DESCRIPTION and layout name tables.
COLLABORA_TRANSFORM_DSL_URL = "https://github.com/CollaboraOnline/online/blob/master/wsd/DocumentToolDescriptions.hpp"

# Impress ``page.Layout`` ids from include/xmloff/autolayout.hxx.
# Deprecated names (CHART, TAB, TEXTOBJ, …) keep their historic numbers.
# The header notes that enum/text/chart/org/clip were merged into content
# placeholders; those ids still round-trip, but several no longer grow a
# chart, table, or clip shape. Notes and handout ids are not slide layouts.
AUTOLAYOUT_ID: dict[str, int] = {
    "AUTOLAYOUT_TITLE": 0,
    "AUTOLAYOUT_TITLE_CONTENT": 1,
    "AUTOLAYOUT_CHART": 2,
    "AUTOLAYOUT_TITLE_2CONTENT": 3,
    "AUTOLAYOUT_TEXTCHART": 4,
    "AUTOLAYOUT_ORG": 5,
    "AUTOLAYOUT_TEXTCLIP": 6,
    "AUTOLAYOUT_CHARTTEXT": 7,
    "AUTOLAYOUT_TAB": 8,
    "AUTOLAYOUT_CLIPTEXT": 9,
    "AUTOLAYOUT_TEXTOBJ": 10,
    "AUTOLAYOUT_OBJ": 11,
    "AUTOLAYOUT_TITLE_CONTENT_2CONTENT": 12,
    "AUTOLAYOUT_OBJTEXT": 13,
    "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT": 14,
    "AUTOLAYOUT_TITLE_2CONTENT_CONTENT": 15,
    "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT": 16,
    "AUTOLAYOUT_TEXTOVEROBJ": 17,
    "AUTOLAYOUT_TITLE_4CONTENT": 18,
    "AUTOLAYOUT_TITLE_ONLY": 19,
    "AUTOLAYOUT_NONE": 20,
    "AUTOLAYOUT_NOTES": 21,
    "AUTOLAYOUT_HANDOUT1": 22,
    "AUTOLAYOUT_HANDOUT2": 23,
    "AUTOLAYOUT_HANDOUT3": 24,
    "AUTOLAYOUT_HANDOUT4": 25,
    "AUTOLAYOUT_HANDOUT6": 26,
    "AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT": 27,
    "AUTOLAYOUT_VTITLE_VCONTENT": 28,
    "AUTOLAYOUT_TITLE_VCONTENT": 29,
    "AUTOLAYOUT_TITLE_2VTEXT": 30,
    "AUTOLAYOUT_HANDOUT9": 31,
    "AUTOLAYOUT_ONLY_TEXT": 32,
    "AUTOLAYOUT_4CLIPART": 33,
    "AUTOLAYOUT_TITLE_6CONTENT": 34,
}

# Collabora AUTOLAYOUT_* names the transform DSL documents
# (DocumentToolDescriptions.hpp). Values are the shared AutoLayout ids above.
_COLLABORA_AUTOLAYOUT_NAMES = (
    "AUTOLAYOUT_TITLE",
    "AUTOLAYOUT_TITLE_CONTENT",
    "AUTOLAYOUT_TITLE_2CONTENT",
    "AUTOLAYOUT_TITLE_CONTENT_2CONTENT",
    "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT",
    "AUTOLAYOUT_TITLE_2CONTENT_CONTENT",
    "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT",
    "AUTOLAYOUT_TITLE_4CONTENT",
    "AUTOLAYOUT_TITLE_ONLY",
    "AUTOLAYOUT_NONE",
    "AUTOLAYOUT_ONLY_TEXT",
    "AUTOLAYOUT_TITLE_6CONTENT",
    "AUTOLAYOUT_VTITLE_VCONTENT",
    "AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT",
    "AUTOLAYOUT_TITLE_VCONTENT",
    "AUTOLAYOUT_TITLE_2VTEXT",
)
AUTOLAYOUT_BY_NAME: dict[str, int] = {name: AUTOLAYOUT_ID[name] for name in _COLLABORA_AUTOLAYOUT_NAMES}

# Embedded for the LLM tool description (from Collabora TRANSFORM_PARAM_DESCRIPTION).
TRANSFORM_PARAM_DESCRIPTION = (
    r"""JSON transformation commands. The top-level object can contain "Transforms" and/or "UnoCommand" objects in any order.

--- Impress/ODP Presentations ---

For presentations, use {"Transforms": {"SlideCommands": [...]}} where SlideCommands is an array of operations applied in order. There is always a "current slide" (default: index 0) that most commands act on. All slides must go in a single SlideCommands array - use InsertMasterSlide to add new slides within the same array. Never send multiple JSON objects.

REQUIRED for every slide: use EditTextObject to bold the title (.uno:Bold), and apply .uno:DefaultBullet to content placeholders that list items. Do NOT prefix text lines with "- " when using DefaultBullet (the bullet is automatic). Do NOT put sub-headings or blank lines inside content placeholders - only the items to be bulleted. Choose the layout that fits the content (see Available layouts below).

Navigation:
- {"JumpToSlide": N} - jump to 0-based slide index; use "last" for last slide
- {"JumpToSlideByName": "name"} - jump to named slide

Slide management (inserts after current slide and jumps to new slide):
- {"InsertMasterSlide": N} - insert slide based on master slide at index N
- {"InsertMasterSlideByName": "name"} - insert slide by master slide name
- {"DeleteSlide": N} - delete slide at index; use "" for current slide
- {"DuplicateSlide": N} - duplicate slide at index; use "" for current
- {"MoveSlide": N} - move current slide to position N
- {"MoveSlide.X": N} - move slide at index X to position N
- {"RenameSlide": "name"} - rename current slide (must be unique)

Layout (applied to current slide):
- {"ChangeLayoutByName": "name"} - set layout by name
- {"ChangeLayout": N} - set layout by numeric ID
Available layouts (use ChangeLayoutByName with these names):
- AUTOLAYOUT_TITLE (id=0) - title + subtitle
- AUTOLAYOUT_TITLE_CONTENT (id=1) - title + one content area
- AUTOLAYOUT_TITLE_2CONTENT (id=3) - title + two content areas side by side
- AUTOLAYOUT_TITLE_CONTENT_2CONTENT (id=12)
- AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT (id=14)
- AUTOLAYOUT_TITLE_2CONTENT_CONTENT (id=15)
- AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT (id=16)
- AUTOLAYOUT_TITLE_4CONTENT (id=18)
- AUTOLAYOUT_TITLE_ONLY (id=19)
- AUTOLAYOUT_NONE (id=20)
- AUTOLAYOUT_ONLY_TEXT (id=32)
- AUTOLAYOUT_TITLE_6CONTENT (id=34)
- AUTOLAYOUT_VTITLE_VCONTENT (id=28)
- AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT (id=27)
- AUTOLAYOUT_TITLE_VCONTENT (id=29)
- AUTOLAYOUT_TITLE_2VTEXT (id=30)

Text content:
- {"SetText.N": "text"} - set text of placeholder N on current slide (0=title, 1=first content, ...). Use \n for paragraph breaks.

Rich text editing:
- {"EditTextObject.N": [...]} - edit text object N with sub-commands (SelectText, SelectParagraph, InsertText, UnoCommand).

WriterAgent V1 does not yet support GenerateImage.N, MarkObject, UnMarkObject, or ContentControls.* — use image_generate or atomic draw tools instead.

Full DSL reference: """
    + COLLABORA_TRANSFORM_DSL_URL
)

_DEFERRED_PREFIXES = ("GenerateImage.", "InsertImageAt.", "InsertImage.", "ContentControls.")
_DEFERRED_EXACT = frozenset({"MarkObject", "UnMarkObject"})


def resolve_layout_id(name_or_id: Any) -> int | None:
    """Resolve a Collabora AUTOLAYOUT name or a numeric Impress page.Layout id.

    Friendly names (``blank``, ``title_only``, …) are resolved by
    ``plugin.draw.transitions.layout_id``, which reads ``AUTOLAYOUT_ID``.
    This helper stays on the transform vocabulary (``AUTOLAYOUT_*`` or a raw id).
    """
    if isinstance(name_or_id, bool):
        return None
    if isinstance(name_or_id, int):
        return name_or_id
    if isinstance(name_or_id, float) and name_or_id.is_integer():
        return int(name_or_id)
    if not isinstance(name_or_id, str):
        return None
    key = name_or_id.strip()
    if not key:
        return None
    upper = key.upper()
    if upper in AUTOLAYOUT_BY_NAME:
        return AUTOLAYOUT_BY_NAME[upper]
    try:
        return int(key)
    except ValueError:
        return None


def is_deferred_command_key(key: str) -> bool:
    if key in _DEFERRED_EXACT:
        return True
    return any(key.startswith(p) for p in _DEFERRED_PREFIXES)


def parse_transform_argument(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse transform JSON string or dict. Returns (obj, error_message)."""
    if raw is None:
        return None, "No transform parameter provided"
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        if not raw.strip():
            return None, "No transform parameter provided"
        obj = safe_json_loads(raw, default=None, strict=True)
        if not isinstance(obj, dict):
            return None, ("Invalid JSON in transform parameter. All slides must be in a single SlideCommands array within one Transforms object. Use InsertMasterSlide to add slides within the same array.")
    else:
        return None, "transform must be a JSON string or object"

    transforms = obj.get("Transforms")
    if transforms is not None and not isinstance(transforms, dict):
        return None, "Transforms must be an object"
    if transforms is not None:
        slide_cmds = transforms.get("SlideCommands")
        if slide_cmds is not None and not isinstance(slide_cmds, list):
            return None, "SlideCommands must be an array"
    return obj, None


def get_slide_commands(transform_obj: dict[str, Any]) -> list[dict[str, Any]]:
    transforms = transform_obj.get("Transforms") or {}
    if not isinstance(transforms, dict):
        return []
    cmds = transforms.get("SlideCommands") or []
    if not isinstance(cmds, list):
        return []
    return [c for c in cmds if isinstance(c, dict)]
