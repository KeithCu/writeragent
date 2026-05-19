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

from plugin.draw.transitions import _LAYOUTS
from plugin.framework.json_utils import safe_json_loads

# Upstream source for TRANSFORM_PARAM_DESCRIPTION and layout name tables.
COLLABORA_TRANSFORM_DSL_URL = "https://github.com/CollaboraOnline/online/blob/master/wsd/DocumentToolDescriptions.hpp"

# Collabora AUTOLAYOUT_* names → Impress page.Layout id (DocumentToolDescriptions.hpp).
AUTOLAYOUT_BY_NAME: dict[str, int] = {
    "AUTOLAYOUT_TITLE": 0,
    "AUTOLAYOUT_TITLE_CONTENT": 1,
    "AUTOLAYOUT_TITLE_2CONTENT": 3,
    "AUTOLAYOUT_TITLE_CONTENT_2CONTENT": 12,
    "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT": 14,
    "AUTOLAYOUT_TITLE_2CONTENT_CONTENT": 15,
    "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT": 16,
    "AUTOLAYOUT_TITLE_4CONTENT": 18,
    "AUTOLAYOUT_TITLE_ONLY": 19,
    "AUTOLAYOUT_NONE": 20,
    "AUTOLAYOUT_ONLY_TEXT": 32,
    "AUTOLAYOUT_TITLE_6CONTENT": 34,
    "AUTOLAYOUT_VTITLE_VCONTENT": 28,
    "AUTOLAYOUT_VTITLE_VCONTENT_OVER_VCONTENT": 27,
    "AUTOLAYOUT_TITLE_VCONTENT": 29,
    "AUTOLAYOUT_TITLE_2VTEXT": 30,
}

# Embedded for the LLM tool description (from Collabora TRANSFORM_PARAM_DESCRIPTION).
TRANSFORM_PARAM_DESCRIPTION = r"""JSON transformation commands. The top-level object can contain "Transforms" and/or "UnoCommand" objects in any order.

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

WriterAgent V1 does not yet support GenerateImage.N, MarkObject, UnMarkObject, or ContentControls.* — use generate_image or atomic draw tools instead.

Full DSL reference: """ + COLLABORA_TRANSFORM_DSL_URL

_DEFERRED_PREFIXES = ("GenerateImage.", "InsertImageAt.", "InsertImage.", "ContentControls.")
_DEFERRED_EXACT = frozenset({"MarkObject", "UnMarkObject"})


def resolve_layout_id(name_or_id: Any) -> int | None:
    """Resolve Collabora AUTOLAYOUT name, WriterAgent layout alias, or numeric id."""
    if isinstance(name_or_id, bool):
        return None
    if isinstance(name_or_id, int):
        return name_or_id
    if isinstance(name_or_id, float) and name_or_id == int(name_or_id):
        return int(name_or_id)
    if not isinstance(name_or_id, str):
        return None
    key = name_or_id.strip()
    if not key:
        return None
    upper = key.upper()
    if upper in AUTOLAYOUT_BY_NAME:
        return AUTOLAYOUT_BY_NAME[upper]
    lower = key.lower()
    if lower in _LAYOUTS:
        return _LAYOUTS[lower]
    if upper.startswith("AUTOLAYOUT_") and upper in AUTOLAYOUT_BY_NAME:
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
            return None, (
                "Invalid JSON in transform parameter. All slides must be in a single SlideCommands "
                "array within one Transforms object. Use InsertMasterSlide to add slides within the same array."
            )
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
