# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Apply Collabora-compatible transform JSON to Draw/Impress documents (PyUNO)."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from plugin.writer.edit_review import WriterCompoundUndo
from plugin.draw.bridge import DrawBridge
from plugin.draw.transform_schema import get_slide_commands, is_deferred_command_key, resolve_layout_id

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_SET_TEXT_RE = re.compile(r"^SetText\.(\d+)$", re.I)
_EDIT_TEXT_RE = re.compile(r"^EditTextObject\.(\d+)$", re.I)
_MOVE_SLIDE_RE = re.compile(r"^MoveSlide\.(\d+)$", re.I)


def _text_shapes(page: Any) -> list[Any]:
    shapes = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if hasattr(shape, "getString") or hasattr(shape, "getText"):
            shapes.append(shape)
    return shapes


def _set_shape_text(shape: Any, text: str) -> None:
    if hasattr(shape, "getText"):
        try:
            xtext = shape.getText()
            xtext.setString(text)
            return
        except Exception:
            pass
    if hasattr(shape, "setString"):
        shape.setString(text)


def _parse_slide_index(val: Any, current: int, page_count: int) -> int | None:
    if val == "" or val is None:
        return current
    if isinstance(val, str) and val.strip().lower() == "last":
        return max(0, page_count - 1)
    try:
        idx = int(val)
        return max(0, min(idx, page_count - 1))
    except (TypeError, ValueError):
        return None


def current_slide_after_move(current: int, move_from: int, move_to: int) -> int:
    """Active page after MoveSlide, matching DrawViewShell.

    ``sd/source/ui/view/drviews2.cxx`` ``FuTransformDocumentStructure`` updates
    ``nNextPageId`` from ``nMoveFrom`` / ``nMoveTo`` / ``nActPageId``:

    - the active page is the one that moved → follow it to ``nMoveTo``
    - a page before the active one is dropped at or after it → ``nActPageId - 1``
    - a page after the active one is dropped at or before it → ``nActPageId + 1``

    Pointing ``current_slide`` at ``nMoveTo`` whenever the move succeeds leaves
    later ``SetText`` / ``EditTextObject`` / ``ChangeLayout`` commands on the
    moved slide. With current 2, ``{"MoveSlide.0": 3}`` must land on 1.
    """
    if current == move_from:
        return move_to
    if move_from < current and move_to >= current:
        return current - 1
    if move_from > current and move_to <= current:
        return current + 1
    return current


def current_slide_after_delete(current: int, deleted: int, page_count: int) -> int:
    """Active page after DeleteSlide.

    LO decrements when ``nPageIdToDel <= nActPageId`` (same function). The
    following command then clamps ``nNextPageId`` into ``[0, page_count)``.
    Clamping only when ``current >= page_count`` leaves a delete of an earlier
    slide (current 2, delete 0 in a 4-slide deck) sitting on the former
    index-3 slide.
    """
    if deleted <= current:
        current -= 1
    if page_count <= 0:
        return 0
    if current < 0:
        return 0
    if current >= page_count:
        return page_count - 1
    return current


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` of each paragraph. The ``\\n`` separator is in neither."""
    spans: list[tuple[int, int]] = []
    start = 0
    for index, ch in enumerate(text):
        if ch == "\n":
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(text)))
    return spans


def _paragraph_point(text: str, para: int, char: int) -> int:
    """Absolute character index of an EditView ``ESelection`` point.

    Draw/Impress shape text does not implement ``XParagraphCursor``
    (``gotoNextParagraph`` / ``gotoEndOfParagraph`` are absent; a query
    returns None). ``getString()`` joins paragraphs with ``\\n``, and
    ``goRight`` counts that newline. ``ESelection`` indices are per
    paragraph and do not include the break (``drviews2.cxx`` SelectText).
    """
    spans = _paragraph_spans(text)
    if para < 0:
        para = 0
    if para >= len(spans):
        para = len(spans) - 1
    begin, end = spans[para]
    length = end - begin
    if char < 0:
        char = 0
    if char > length:
        char = length
    return begin + char


def text_selection_bounds(text: str, spec: Any) -> tuple[int, int] | None:
    """``[start, end)`` for a ``SelectText`` spec. None selects the whole string.

    Spec shapes follow ``drviews2.cxx``: ``[]`` all text, ``[para]`` that
    paragraph, ``[para, char]`` a collapsed cursor, three ints (end char is
    the paragraph end), four ints ``[startPara, startChar, endPara, endChar]``.
    """
    if spec == [] or spec is None:
        return None
    if not isinstance(spec, list):
        raise ValueError("SelectText spec must be a list")
    values = [int(item) for item in spec]
    if len(values) == 1:
        return _paragraph_point(text, values[0], 0), _paragraph_point(text, values[0], 10**9)
    if len(values) == 2:
        point = _paragraph_point(text, values[0], values[1])
        return point, point
    if len(values) == 3:
        values.append(10**9)
    if len(values) >= 4:
        start = _paragraph_point(text, values[0], values[1])
        end = _paragraph_point(text, values[2], values[3])
        if end < start:
            start, end = end, start
        return start, end
    raise ValueError("SelectText spec has no coordinates")


def insert_text_leave_selected(cursor: Any, text: str) -> Any:
    """Replace the cursor and leave *text* selected.

    LO's ``EditView::InsertText(aText, true)`` (``drviews2.cxx``) selects the
    insertion so the next ``.uno:Bold`` / ``.uno:Italic`` hits that span.
    ``XTextCursor.setString`` on some builds collapses the cursor at the end,
    and a collapsed cursor has no range for the following format command.
    """
    cursor.setString(text)
    if not text:
        return cursor
    collapsed = True
    try:
        collapsed = bool(cursor.isCollapsed())
    except Exception:
        collapsed = True
    try:
        selected = cursor.getString()
    except Exception:
        selected = None
    if collapsed or selected != text:
        try:
            cursor.goLeft(len(text), True)
        except Exception:
            return cursor
    return cursor


# ParagraphAdjust (com.sun.star.style): LEFT, RIGHT, BLOCK, CENTER.
_PARA_ADJUST_BY_COMMAND = {
    ".uno:LeftPara": 0,
    ".uno:RightPara": 1,
    ".uno:JustifyPara": 2,
    ".uno:CenterPara": 3,
}


def _cursor_prop(cursor: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(cursor, name)
    except Exception:
        return default


def _set_cursor_prop(cursor: Any, name: str, value: Any) -> None:
    setattr(cursor, name, value)


def _is_italic(value: Any) -> bool:
    label = getattr(value, "value", None)
    if isinstance(label, str) and label.upper() in ("ITALIC", "OBLIQUE"):
        return True
    text = str(value).upper()
    return "ITALIC" in text or "OBLIQUE" in text


def _font_slant(name: str) -> Any:
    import uno

    return uno.Enum("com.sun.star.awt.FontSlant", name)


def _toggle_numbering(cursor: Any) -> None:
    """``.uno:DefaultBullet`` / ``.uno:DefaultNumbering`` flip ``NumberingLevel``.

    Dispatching either command with the shape selected sets ``NumberingLevel``
    on every paragraph (probed: None → 0). ``-1`` clears it (read-back is
    None). The placeholder's own numbering rules decide bullet versus digit.
    """
    level = _cursor_prop(cursor, "NumberingLevel", None)
    try:
        enabled = level is not None and int(level) >= 0
    except (TypeError, ValueError):
        enabled = False
    _set_cursor_prop(cursor, "NumberingLevel", -1 if enabled else 0)


def _color_from_arguments(arguments: dict[str, Any] | None) -> int | None:
    if not arguments:
        return None
    for key in ("Color.Color", "Color"):
        if key not in arguments:
            continue
        raw: Any = arguments[key]
        if isinstance(raw, dict):
            raw = raw.get("value")
        if isinstance(raw, str):
            try:
                return int(raw)
            except ValueError:
                return None
        # bool is an int subclass; a JSON true is not a color.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return int(raw)
    return None


def apply_text_cursor_command(cursor: Any, uno_name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Apply a text ``.uno:`` command to *cursor*'s selection. False if unknown.

    ``controller.select(shape)`` then ``executeDispatch(".uno:Bold")`` bolds
    the entire text object. Selecting the ``XTextCursor`` does not change
    that: the dispatcher reads the EditView selection, which headless Draw
    never enters. Setting ``CharWeight`` / ``CharPosture`` / paragraph
    properties on the cursor changes only the selected range.
    """
    name = uno_name.strip()
    if name == ".uno:Bold":
        weight = _cursor_prop(cursor, "CharWeight", 0)
        try:
            bold = float(weight) >= 150.0
        except (TypeError, ValueError):
            bold = False
        _set_cursor_prop(cursor, "CharWeight", 100.0 if bold else 150.0)
        return True
    if name == ".uno:Italic":
        italic = _is_italic(_cursor_prop(cursor, "CharPosture", None))
        _set_cursor_prop(cursor, "CharPosture", _font_slant("NONE" if italic else "ITALIC"))
        return True
    if name == ".uno:Underline":
        try:
            on = int(_cursor_prop(cursor, "CharUnderline", 0) or 0) != 0
        except (TypeError, ValueError):
            on = False
        _set_cursor_prop(cursor, "CharUnderline", 0 if on else 1)
        return True
    if name == ".uno:Strikeout":
        try:
            on = int(_cursor_prop(cursor, "CharStrikeout", 0) or 0) != 0
        except (TypeError, ValueError):
            on = False
        _set_cursor_prop(cursor, "CharStrikeout", 0 if on else 1)
        return True
    if name == ".uno:Shadowed":
        on = bool(_cursor_prop(cursor, "CharShadowed", False))
        _set_cursor_prop(cursor, "CharShadowed", not on)
        return True
    if name in (".uno:SuperScript", ".uno:SubScript"):
        try:
            escapement = int(_cursor_prop(cursor, "CharEscapement", 0) or 0)
        except (TypeError, ValueError):
            escapement = 0
        want = 14000 if name == ".uno:SuperScript" else -14000
        if (want > 0 and escapement > 0) or (want < 0 and escapement < 0):
            _set_cursor_prop(cursor, "CharEscapement", 0)
            _set_cursor_prop(cursor, "CharEscapementHeight", 100)
        else:
            _set_cursor_prop(cursor, "CharEscapement", want)
            _set_cursor_prop(cursor, "CharEscapementHeight", 58)
        return True
    if name in _PARA_ADJUST_BY_COMMAND:
        _set_cursor_prop(cursor, "ParaAdjust", _PARA_ADJUST_BY_COMMAND[name])
        return True
    if name in (".uno:DefaultBullet", ".uno:DefaultNumbering"):
        _toggle_numbering(cursor)
        return True
    if name == ".uno:Color":
        color = _color_from_arguments(arguments)
        if color is None:
            return False
        _set_cursor_prop(cursor, "CharColor", color)
        return True
    return False


class SlideCommandEngine:
    """Execute SlideCommands array against a Draw/Impress document."""

    tctx: ToolContext
    doc: Any
    bridge: DrawBridge
    pages: Any
    current_slide: int

    def __init__(self, tctx: ToolContext) -> None:
        self.tctx = tctx
        self.doc = tctx.doc
        self.bridge = DrawBridge(self.doc)
        self.pages = self.bridge.get_pages()
        self.current_slide = tctx.active_page_index if tctx.active_page_index is not None else self.bridge.get_active_page_index()
        self.applied: list[str] = []
        self.warnings: list[str] = []

    def apply(self, transform_obj: dict[str, Any]) -> dict[str, Any]:
        try:
            with WriterCompoundUndo(self.doc, "WriterAgent: Transform document structure"):
                top_uno = transform_obj.get("UnoCommand")
                if top_uno is not None:
                    self._apply_top_level_uno(top_uno)
                for cmd in get_slide_commands(transform_obj):
                    self._apply_command(cmd)
                self.bridge.set_current_page_index(self.current_slide)
                return {"status": "ok", "current_slide": self.current_slide, "applied": self.applied, "warnings": self.warnings}
        except Exception as exc:
            log.exception("SlideCommandEngine.apply failed")
            return {"status": "error", "message": str(exc), "applied": self.applied, "warnings": self.warnings}

    def _page_count(self) -> int:
        return self.pages.getCount()

    def _current_page(self) -> Any:
        return self.pages.getByIndex(self.current_slide)

    def _apply_command(self, cmd: dict[str, Any]) -> None:
        for key, value in cmd.items():
            if is_deferred_command_key(key):
                self.warnings.append("%s is not supported in WriterAgent V1; use image_generate or atomic draw tools." % key)
                continue
            if key == "JumpToSlide":
                idx = _parse_slide_index(value, self.current_slide, self._page_count())
                if idx is not None:
                    self.current_slide = idx
                    self.applied.append("JumpToSlide:%d" % idx)
                else:
                    self.warnings.append("Invalid JumpToSlide: %r" % value)
            elif key == "JumpToSlideByName":
                found = self._jump_to_name(str(value))
                if found is None:
                    self.warnings.append("JumpToSlideByName: slide not found: %r" % value)
            elif key == "InsertMasterSlide":
                self._insert_master(master_index=int(value))
            elif key == "InsertMasterSlideByName":
                self._insert_master(master_name=str(value))
            elif key == "DeleteSlide":
                self._delete_slide(value)
            elif key == "DuplicateSlide":
                self._duplicate_slide(value)
            elif key == "MoveSlide":
                self._move_slide(self.current_slide, int(value))
            elif key == "RenameSlide":
                if self.bridge.rename_slide(self.current_slide, str(value)):
                    self.applied.append("RenameSlide:%s" % value)
                else:
                    self.warnings.append("RenameSlide failed")
            elif key == "ChangeLayoutByName":
                self._set_layout(resolve_layout_id(value), key)
            elif key == "ChangeLayout":
                self._set_layout(resolve_layout_id(value), key)
            elif key == "UnoCommand":
                self._dispatch_uno_string(value)
                self.applied.append("UnoCommand:%s" % value)
            else:
                m = _SET_TEXT_RE.match(key)
                if m:
                    self._set_text_index(int(m.group(1)), str(value))
                    continue
                m = _EDIT_TEXT_RE.match(key)
                if m:
                    if isinstance(value, list):
                        self._edit_text_object(int(m.group(1)), value)
                    else:
                        self.warnings.append("%s requires an array of sub-commands" % key)
                    continue
                m = _MOVE_SLIDE_RE.match(key)
                if m:
                    self._move_slide(int(m.group(1)), int(value))
                    continue
                self.warnings.append("Unknown or unsupported command key: %s" % key)

    def _jump_to_name(self, name: str) -> int | None:
        for i in range(self._page_count()):
            page = self.pages.getByIndex(i)
            try:
                if hasattr(page, "Name") and page.Name == name:
                    self.current_slide = i
                    self.applied.append("JumpToSlideByName:%s" % name)
                    return i
            except Exception:
                pass
        return None

    def _insert_master(self, master_index: int | None = None, master_name: str | None = None) -> None:
        _unused, new_idx = self.bridge.insert_slide_from_master(master_index=master_index, master_name=master_name, after_index=self.current_slide, switch=True)
        self.current_slide = new_idx
        self.pages = self.bridge.get_pages()
        self.applied.append("InsertMasterSlide:%d" % new_idx)

    def _delete_slide(self, val: Any) -> None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count())
        if idx is None:
            self.warnings.append("Invalid DeleteSlide: %r" % val)
            return
        if self._page_count() <= 1:
            self.warnings.append("Cannot delete the only slide")
            return
        self.bridge.delete_slide(idx)
        self.pages = self.bridge.get_pages()
        self.current_slide = current_slide_after_delete(self.current_slide, idx, self._page_count())
        self.applied.append("DeleteSlide:%d" % idx)

    def _duplicate_slide(self, val: Any) -> None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count())
        if idx is None:
            self.warnings.append("Invalid DuplicateSlide: %r" % val)
            return
        self.bridge.duplicate_slide(idx, switch=True)
        self.pages = self.bridge.get_pages()
        self.current_slide = min(idx + 1, self._page_count() - 1)
        self.applied.append("DuplicateSlide:%d" % idx)

    def _move_slide(self, from_idx: int, to_idx: int) -> None:
        if self.bridge.move_slide(from_idx, to_idx):
            self.pages = self.bridge.get_pages()
            self.current_slide = current_slide_after_move(self.current_slide, from_idx, to_idx)
            self.applied.append("MoveSlide:%d->%d" % (from_idx, to_idx))
        else:
            self.warnings.append("MoveSlide failed %d -> %d" % (from_idx, to_idx))

    def _set_layout(self, layout_id: int | None, key: str) -> None:
        if layout_id is None:
            self.warnings.append("Unknown layout in %s" % key)
            return
        page = self._current_page()
        page.Layout = layout_id
        self.applied.append("%s:%d" % (key, layout_id))

    def _set_text_index(self, shape_index: int, text: str) -> None:
        shapes = _text_shapes(self._current_page())
        if shape_index < 0 or shape_index >= len(shapes):
            self.warnings.append("SetText.%d: shape index out of range (have %d text shapes)" % (shape_index, len(shapes)))
            return
        _set_shape_text(shapes[shape_index], text)
        self.applied.append("SetText.%d" % shape_index)

    def _edit_text_object(self, shape_index: int, subcmds: list[Any]) -> None:
        shapes = _text_shapes(self._current_page())
        if shape_index < 0 or shape_index >= len(shapes):
            self.warnings.append("EditTextObject.%d: shape index out of range" % shape_index)
            return
        shape = shapes[shape_index]
        cursor = None
        xtext = None
        if hasattr(shape, "getText"):
            try:
                xtext = shape.getText()
                cursor = xtext.createTextCursor()
            except Exception as exc:
                self.warnings.append("EditTextObject.%d: no text: %s" % (shape_index, exc))
                return
        for sub in subcmds:
            if not isinstance(sub, dict):
                continue
            for sk, sv in sub.items():
                if sk == "SelectText":
                    cursor = self._select_text(xtext, cursor, sv)
                elif sk == "SelectParagraph":
                    cursor = self._select_paragraph(xtext, cursor, int(sv))
                elif sk == "InsertText":
                    if cursor is not None and xtext is not None:
                        cursor = insert_text_leave_selected(cursor, str(sv))
                    elif hasattr(shape, "setString"):
                        shape.setString(str(sv))
                elif sk == "UnoCommand":
                    self._dispatch_uno_string(sv, cursor=cursor, shape=shape)
        self.applied.append("EditTextObject.%d" % shape_index)

    def _select_text(self, xtext: Any, cursor: Any, spec: Any) -> Any:
        if cursor is None or xtext is None:
            return cursor
        try:
            text = xtext.getString()
        except Exception as exc:
            self.warnings.append("SelectText failed: %s" % exc)
            return cursor
        if not isinstance(text, str):
            text = str(text)
        try:
            bounds = text_selection_bounds(text, spec)
        except (TypeError, ValueError) as exc:
            self.warnings.append("SelectText failed: %s" % exc)
            return cursor
        try:
            cursor.gotoStart(False)
            if bounds is None:
                cursor.gotoEnd(True)
                return cursor
            start, end = bounds
            if start:
                cursor.goRight(start, False)
            if end > start:
                cursor.goRight(end - start, True)
        except Exception as exc:
            self.warnings.append("SelectText failed: %s" % exc)
        return cursor

    def _select_paragraph(self, xtext: Any, cursor: Any, para_index: int) -> Any:
        return self._select_text(xtext, cursor, [para_index])

    def _apply_top_level_uno(self, uno_spec: Any) -> None:
        if isinstance(uno_spec, dict):
            name = uno_spec.get("name") or uno_spec.get("Name")
            args = uno_spec.get("arguments") or uno_spec.get("Arguments") or {}
            self._dispatch_uno_named(str(name), args)
            self.applied.append("UnoCommand:%s" % name)
        else:
            self._dispatch_uno_string(uno_spec)
            self.applied.append("UnoCommand")

    def _dispatch_uno_string(self, cmd: Any, cursor: Any = None, shape: Any = None) -> None:
        if not isinstance(cmd, str):
            return
        cmd = cmd.strip()
        if not cmd:
            return
        # ".uno:Bold" or '.uno:Color {"Color.Color":...}'
        parts = cmd.split(None, 1)
        uno_name = parts[0]
        arg_json = parts[1] if len(parts) > 1 else None
        arguments: dict[str, Any] | None = None
        if arg_json:
            from plugin.framework.json_utils import safe_json_loads

            parsed = safe_json_loads(arg_json, default={})
            if isinstance(parsed, dict):
                arguments = parsed
        # A text cursor from EditTextObject is not the view selection.
        # controller.select(shape) makes .uno:Bold format every character
        # in the object (probed on an Impress title shape). Apply the
        # command to the cursor range instead of dispatching.
        if cursor is not None:
            try:
                handled = apply_text_cursor_command(cursor, uno_name, arguments)
            except Exception as exc:
                self.warnings.append("UnoCommand %s failed: %s" % (uno_name, exc))
                return
            if not handled:
                self.warnings.append("UnoCommand %s does not apply to the text selection" % uno_name)
            return
        props = self._uno_props_from_dict(arguments) if arguments else ()
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                return
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            if shape is not None:
                try:
                    controller.select(shape)
                except Exception:
                    pass
            dispatcher.executeDispatch(frame, uno_name, "", 0, props)
        except Exception as exc:
            self.warnings.append("UnoCommand %s failed: %s" % (uno_name, exc))

    def _dispatch_uno_named(self, name: str, arguments: dict[str, Any]) -> None:
        props = self._uno_props_from_dict(arguments)
        try:
            controller = self.doc.getCurrentController()
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            dispatcher.executeDispatch(frame, name, "", 0, props)
        except Exception as exc:
            self.warnings.append("UnoCommand %s failed: %s" % (name, exc))

    def _uno_props_from_dict(self, arguments: dict[str, Any]) -> tuple[Any, ...]:
        from com.sun.star.beans import PropertyValue

        props = []
        for arg_name, spec in arguments.items():
            if isinstance(spec, dict) and "value" in spec:
                val = spec["value"]
                if spec.get("type") == "boolean" and isinstance(val, str):
                    val = val.lower() in ("true", "1", "yes")
                elif spec.get("type") in ("long", "int") and not isinstance(val, int):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        pass
                elif spec.get("type") == "float":
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        pass
            else:
                val = spec
            props.append(PropertyValue(arg_name, 0, val, 0))
        return tuple(props)
