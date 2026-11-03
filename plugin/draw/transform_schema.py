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

import re
from typing import Any

from plugin.draw.layouts import LAYOUT_TABLE, layout_id
from plugin.framework.json_utils import safe_json_loads

# Upstream source for TRANSFORM_PARAM_DESCRIPTION and layout name tables.
COLLABORA_TRANSFORM_DSL_URL = "https://github.com/CollaboraOnline/online/blob/master/wsd/DocumentToolDescriptions.hpp"

# Exact command names supported in SlideCommands.
EXACT_COMMAND_KEYS = frozenset({
    "JumpToSlide",
    "JumpToSlideByName",
    "InsertMasterSlide",
    "InsertMasterSlideByName",
    "DeleteSlide",
    "DuplicateSlide",
    "MoveSlide",
    "RenameSlide",
    "ChangeLayout",
    "ChangeLayoutByName",
    "UnoCommand",
})

SET_TEXT_RE = re.compile(r"^SetText\.(\d+)$", re.I)
EDIT_TEXT_RE = re.compile(r"^EditTextObject\.(\d+)$", re.I)
MOVE_SLIDE_RE = re.compile(r"^MoveSlide\.(\d+)$", re.I)

# Collabora AUTOLAYOUT_* names the transform DSL documents
# (DocumentToolDescriptions.hpp). Values are the shared AutoLayout ids in layouts.py.
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
_BLURB_BY_AUTO = {auto: blurb for _friendly, auto, _id, _aliases, blurb in LAYOUT_TABLE if blurb}
_LAYOUT_BULLETS = "\n".join(
    f"- {name} (id={layout_id(name)})" + (f" - {_BLURB_BY_AUTO[name]}" if name in _BLURB_BY_AUTO else "")
    for name in _COLLABORA_AUTOLAYOUT_NAMES
)

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
"""
    + _LAYOUT_BULLETS
    + r"""

Text content:
- {"SetText.N": "text"} - set text of shape object index N on current slide (a stock title layout has the title at 0 only because it is object 0). Use \n for paragraph breaks.

Rich text editing:
- {"EditTextObject.N": [...]} - edit text object N with sub-commands (SelectText, SelectParagraph, InsertText, UnoCommand).

WriterAgent V1 does not yet support GenerateImage.N, MarkObject, UnMarkObject, or ContentControls.* — use image_generate or atomic draw tools instead.

Full DSL reference: """
    + COLLABORA_TRANSFORM_DSL_URL
)

_DEFERRED_PREFIXES = ("GenerateImage.", "InsertImageAt.", "InsertImage.", "ContentControls.")
_DEFERRED_EXACT = frozenset({"MarkObject", "UnMarkObject"})


def is_deferred_command_key(key: str) -> bool:
    if key in _DEFERRED_EXACT:
        return True
    return any(key.startswith(p) for p in _DEFERRED_PREFIXES)


def classify_command_key(key: Any) -> str | None:
    """Classify a slide command key into a known category or None if unknown."""
    if not isinstance(key, str):
        return None
    if is_deferred_command_key(key):
        return "deferred"
    if key in EXACT_COMMAND_KEYS:
        return key
    if SET_TEXT_RE.match(key):
        return "SetText"
    if EDIT_TEXT_RE.match(key):
        return "EditTextObject"
    if MOVE_SLIDE_RE.match(key):
        return "MoveSlide"
    return None


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


def coerce_int(val: Any) -> int | None:
    """Coerce an int, integral float, or digit string to int. Reject bools."""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if val.is_integer() else None
    if isinstance(val, str):
        s = val.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
                return int(f) if f.is_integer() else None
            except ValueError:
                return None
    return None


def parse_slide_index_token(val: Any) -> tuple[str, int | None] | None:
    """Parse slide index token into ('current', None), ('last', None), ('index', N), or None."""
    if val == "" or val is None:
        return ("current", None)
    if isinstance(val, bool):
        return None
    if isinstance(val, str) and val.strip().lower() == "last":
        return ("last", None)
    try:
        return ("index", int(val))
    except (TypeError, ValueError):
        return None


def split_uno_command(cmd: str) -> tuple[str, dict[str, Any]] | str:
    """Split and parse UNO command string with optional JSON arguments.

    Returns (command_name, args_dict) or an error message string.
    """
    stripped = cmd.strip()
    if not stripped:
        return "UnoCommand requires a non-empty string: %r" % (cmd,)
    parts = stripped.split(None, 1)
    name = parts[0]
    if len(parts) == 1:
        return (name, {})
    rest = parts[1]
    loaded = safe_json_loads(rest, default=None)
    if not isinstance(loaded, dict):
        return "UnoCommand %s has invalid JSON arguments: %r" % (name, rest)
    return (name, loaded)


def validate_top_level_uno(spec: Any) -> str | None:
    """Validate top-level UnoCommand parameter shape before execution."""
    if isinstance(spec, str):
        res = split_uno_command(spec)
        if isinstance(res, str):
            return res
        return None
    if isinstance(spec, dict):
        name = spec.get("name") if "name" in spec else spec.get("Name")
        if not isinstance(name, str) or not name.strip():
            return "Invalid UnoCommand specification: %r" % (spec,)
        args = spec.get("arguments") if "arguments" in spec else spec.get("Arguments")
        if args is not None and not isinstance(args, dict):
            return "Invalid UnoCommand specification: %r" % (spec,)
        return None
    return "Invalid UnoCommand specification: %r" % (spec,)


def validate_slide_commands(transform_obj: Any) -> tuple[str | None, int | None]:
    """Validate Transforms.SlideCommands array before performing any UNO operations.

    Returns (None, None) if valid, or (error_message, failed_at_index).
    """
    if not isinstance(transform_obj, dict):
        return None, None
    transforms = transform_obj.get("Transforms")
    if not isinstance(transforms, dict):
        return None, None
    cmds = transforms.get("SlideCommands")
    if not isinstance(cmds, list):
        return None, None

    for idx, entry in enumerate(cmds):
        if not isinstance(entry, dict) or not entry:
            return f"SlideCommands[{idx}] must be a non-empty object with one key", idx
        if len(entry) != 1:
            return f"SlideCommands[{idx}] must have exactly one command key", idx

        key = next(iter(entry.keys()))
        if not isinstance(key, str):
            return f"SlideCommands[{idx}] command key must be a string", idx

        val = entry[key]

        if is_deferred_command_key(key):
            return (
                "%s is not supported in WriterAgent V1; use image_generate or atomic draw tools."
                % key,
                idx,
            )

        cmd_kind = classify_command_key(key)
        if cmd_kind is None:
            return "Unknown or unsupported command key: %s" % key, idx

        # Value shape validations for commands that do not need the document
        if key in ("JumpToSlide", "DeleteSlide", "DuplicateSlide"):
            if parse_slide_index_token(val) is None:
                return "Invalid %s: %r" % (key, val), idx

        elif key in ("JumpToSlideByName", "InsertMasterSlideByName", "RenameSlide"):
            if not isinstance(val, str):
                return "%s requires a string" % key, idx

        elif key in ("InsertMasterSlide", "MoveSlide") or cmd_kind == "MoveSlide":
            if coerce_int(val) is None:
                return "Invalid %s index: %r" % (key, val), idx

        elif key in ("ChangeLayout", "ChangeLayoutByName"):
            lid = layout_id(val)
            if lid is None:
                return "Unknown layout in %s" % key, idx

        elif cmd_kind == "SetText":
            if not isinstance(val, str):
                return "%s requires a string" % key, idx

        elif key == "UnoCommand":
            if not isinstance(val, str):
                return "UnoCommand requires a string", idx
            uno_res = split_uno_command(val)
            if isinstance(uno_res, str):
                return uno_res, idx

        elif cmd_kind == "EditTextObject":
            if not isinstance(val, list) or len(val) == 0:
                return "%s requires an array of sub-commands" % key, idx
            for sub in val:
                if not isinstance(sub, dict) or len(sub) != 1:
                    return "%s: sub-command must be an object with one key: %r" % (key, sub), idx
                sub_k = next(iter(sub.keys()))
                if not isinstance(sub_k, str):
                    return "%s: sub-command key must be a string: %r" % (key, sub_k), idx
                sub_v = sub[sub_k]

                if sub_k == "SelectText":
                    if sub_v is None or not isinstance(sub_v, list):
                        return "SelectText requires a list or empty array: %r" % (sub_v,), idx
                    if len(sub_v) > 4:
                        return "SelectText requires 0 to 4 integers: %r" % (sub_v,), idx
                    int_sub_v: list[int] = []
                    for item in sub_v:
                        coerced = coerce_int(item)
                        if coerced is None:
                            return "Invalid non-int element in SelectText: %r" % (item,), idx
                        int_sub_v.append(coerced)
                    n = len(int_sub_v)
                    if n == 1:
                        if int_sub_v[0] < 0:
                            return "SelectText: negative paragraph index %d" % int_sub_v[0], idx
                    elif n == 2:
                        if int_sub_v[0] < 0 or int_sub_v[1] < 0:
                            return "SelectText: negative index [%d, %d]" % (int_sub_v[0], int_sub_v[1]), idx
                    elif n == 3:
                        p, c, ep = int_sub_v[0], int_sub_v[1], int_sub_v[2]
                        if p < 0 or c < 0 or ep < 0 or ep < p:
                            return "SelectText: negative or invalid span [%d, %d, %d]" % (p, c, ep), idx
                    elif n == 4:
                        p, start, ep, end_char = int_sub_v[0], int_sub_v[1], int_sub_v[2], int_sub_v[3]
                        if p < 0 or start < 0 or ep < 0 or end_char < 0:
                            return "SelectText: negative coordinates [%d, %d, %d, %d]" % (p, start, ep, end_char), idx
                        if ep < p or (ep == p and end_char < start):
                            return "SelectText: negative span [%d, %d] to [%d, %d]" % (p, start, ep, end_char), idx

                elif sub_k == "SelectParagraph":
                    coerced_p = coerce_int(sub_v)
                    if coerced_p is None:
                        return "Invalid SelectParagraph index: %r" % (sub_v,), idx
                    if coerced_p < 0:
                        return "SelectParagraph: negative paragraph index %d" % coerced_p, idx

                elif sub_k == "InsertText":
                    if not isinstance(sub_v, str):
                        return "InsertText requires a string: %r" % (sub_v,), idx

                elif sub_k == "UnoCommand":
                    if not isinstance(sub_v, str):
                        return "UnoCommand requires a string", idx
                    sub_uno_res = split_uno_command(sub_v)
                    if isinstance(sub_uno_res, str):
                        return sub_uno_res, idx

                else:
                    return "Unknown EditTextObject sub-command: %s" % sub_k, idx

    return None, None
