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

from plugin.draw.bridge import DrawBridge
from plugin.draw.transform_schema import (
    AUTOLAYOUT_ID,
    get_slide_commands,
    is_deferred_command_key,
    resolve_layout_id,
)
from plugin.framework.errors import is_disposed_exception
from plugin.writer.edit_review import WriterCompoundUndo

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_SET_TEXT_RE = re.compile(r"^SetText\.(\d+)$", re.I)
_EDIT_TEXT_RE = re.compile(r"^EditTextObject\.(\d+)$", re.I)
_MOVE_SLIDE_RE = re.compile(r"^MoveSlide\.(\d+)$", re.I)
_VALID_LAYOUT_IDS = frozenset(AUTOLAYOUT_ID.values())


def _set_shape_text(shape: Any, text: str) -> bool:
    if hasattr(shape, "getText") and callable(getattr(shape, "getText", None)):
        try:
            xtext = shape.getText()
            if xtext is not None and hasattr(xtext, "setString") and callable(getattr(xtext, "setString", None)):
                xtext.setString(text)
                # Ensure \n creates paragraph breaks if setString left a single paragraph.
                if "\n" in text and hasattr(xtext, "createEnumeration") and callable(getattr(xtext, "createEnumeration", None)):
                    enum = xtext.createEnumeration()
                    para_count = 0
                    while enum.hasMoreElements():
                        enum.nextElement()
                        para_count += 1
                    if para_count <= 1:
                        lines = text.split("\n")
                        xtext.setString(lines[0])
                        cursor = xtext.createTextCursor()
                        cursor.gotoEnd(False)
                        for line in lines[1:]:
                            try:
                                from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore
                            except Exception:
                                PARAGRAPH_BREAK = 0
                            try:
                                xtext.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                            except Exception:
                                pass
                            cursor.setString(line)
                            cursor.gotoEnd(False)
                return True
        except Exception as e:
            if is_disposed_exception(e):
                raise
            # non-dispose falls through
    if hasattr(shape, "setString") and callable(getattr(shape, "setString", None)):
        try:
            shape.setString(text)
            return True
        except Exception as e:
            if is_disposed_exception(e):
                raise
    return False


def _parse_slide_index(val: Any, current: int, page_count: int, *, clamp: bool = True) -> int | None:
    """Slide index from "", "last", or a number. None when it is not valid.

    clamp=False rejects an out-of-range number. DeleteSlide and DuplicateSlide
    use that: clamping made {"DeleteSlide": 99} delete the last slide.
    """
    if isinstance(val, bool):
        return None
    if val == "" or val is None:
        return current
    if isinstance(val, str) and val.strip().lower() == "last":
        return max(0, page_count - 1)
    try:
        idx = int(val)
    except (TypeError, ValueError):
        return None
    if clamp:
        return max(0, min(idx, page_count - 1))
    if idx < 0 or idx >= page_count:
        return None
    return idx


def _current_after_move(current: int, from_idx: int, to_idx: int) -> int:
    """Index of the same logical slide after MoveSlide.

    A successful move does not set ``current_slide`` to the
    destination. ``{"MoveSlide.0": 3}`` while sitting on slide 2
    would leave the cursor on the slide that was moved.
    LibreOffice moves page *objects* and the view follows the page
    that was current. ``DrawViewShell::FuTemporary``
    (``sd/source/ui/view/drviews2.cxx``, MoveSlide) only jumps to
    ``nMoveTo`` when that page *is* the current one; a page that
    crosses the current index shifts the current index by one.
    """
    if current == from_idx:
        return to_idx
    if from_idx < current and to_idx >= current:
        return current - 1
    if from_idx > current and to_idx <= current:
        return current + 1
    return current


def _current_after_delete(current: int, deleted: int, page_count_after: int) -> int:
    """Index LibreOffice keeps selected after DeleteSlide.

    Deleting a slide at or before the current index must decrement
    ``current_slide``. Leaving it unchanged names the next physical
    slot. Clamping only when the index is past the new end misses
    that shift. ``DrawViewShell::FuTemporary``
    (``sd/source/ui/view/drviews2.cxx``, DeleteSlide) decrements
    whenever ``nPageIdToDel <= nActPageId``, then the next command
    clamps onto a page that still exists.
    """
    if deleted <= current:
        current -= 1
    if page_count_after <= 0:
        return 0
    if current < 0:
        return 0
    if current >= page_count_after:
        return page_count_after - 1
    return current


def _cursor_string(obj: Any) -> str | None:
    if obj is None:
        return None
    getter = getattr(obj, "getString", None)
    if callable(getter):
        try:
            val = getter()
        except Exception as e:
            if is_disposed_exception(e):
                raise
            return None
        if isinstance(val, str):
            return val
    val = getattr(obj, "String", None)
    if isinstance(val, str):
        return val
    return None


def _shape_text_string(shape: Any) -> str | None:
    if shape is None:
        return None
    getter = getattr(shape, "getText", None)
    if callable(getter):
        try:
            text = getter()
        except Exception as e:
            if is_disposed_exception(e):
                raise
            text = None
        found = _cursor_string(text)
        if found is not None:
            return found
    return _cursor_string(shape)


def _text_cursor_is_partial_selection(cursor: Any, shape: Any) -> bool:
    """True when *cursor* covers a range shorter than the whole shape.

    An empty ``createTextCursor()`` is not a range: ``.uno:Bold`` still
    formats the shape. ``SelectText: []`` covers the whole string, which
    is the same as selecting the shape.
    """
    selected = _cursor_string(cursor)
    if not selected:
        return False
    full = _shape_text_string(shape)
    if full is not None and selected == full:
        return False
    return True


# Numeric UNO literals so range formatting does not need a live office.
# awt.FontWeight.BOLD = 150; awt.FontSlant.ITALIC = 2; FontUnderline.SINGLE = 1;
# FontStrikeout.SINGLE = 1; style.ParagraphAdjust LEFT/RIGHT/BLOCK/CENTER = 0/1/2/3.
# Superscript defaults match editeng (33% escapement, 58% height).
_CURSOR_UNO_PROPS: dict[str, tuple[tuple[str, Any], ...]] = {
    ".uno:Bold": (("CharWeight", 150.0),),
    ".uno:Italic": (("CharPosture", 2),),
    ".uno:Underline": (("CharUnderline", 1),),
    ".uno:Strikeout": (("CharStrikeout", 1),),
    ".uno:Shadowed": (("CharShadowed", True),),
    ".uno:SuperScript": (("CharEscapement", 33), ("CharEscapementHeight", 58)),
    ".uno:SubScript": (("CharEscapement", -33), ("CharEscapementHeight", 58)),
    ".uno:LeftPara": (("ParaAdjust", 0),),
    ".uno:RightPara": (("ParaAdjust", 1),),
    ".uno:JustifyPara": (("ParaAdjust", 2),),
    ".uno:CenterPara": (("ParaAdjust", 3),),
}


def _uno_scalar(spec: Any) -> Any:
    if isinstance(spec, dict) and "value" in spec:
        return spec["value"]
    return spec


def _cursor_format_pairs(uno_name: str, arguments: dict[str, Any]) -> tuple[tuple[str, Any], ...] | None:
    mapped = _CURSOR_UNO_PROPS.get(uno_name)
    if mapped is not None:
        return mapped
    if uno_name == ".uno:Color":
        val = _uno_scalar(arguments.get("Color.Color"))
        if isinstance(val, int) and not isinstance(val, bool):
            return (("CharColor", val),)
    if uno_name == ".uno:CharBackColor":
        val = _uno_scalar(arguments.get("CharBackColor.Color"))
        if isinstance(val, int) and not isinstance(val, bool):
            return (("CharBackColor", val),)
    return None


def _set_text_prop(cursor: Any, name: str, value: Any) -> bool:
    setter = getattr(cursor, "setPropertyValue", None)
    if callable(setter):
        try:
            setter(name, value)
            return True
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.debug("cursor setPropertyValue %s failed", name, exc_info=True)
    try:
        setattr(cursor, name, value)
    except Exception as e:
        if is_disposed_exception(e):
            raise
        log.debug("cursor attribute %s failed", name, exc_info=True)
        return False
    return True


def _apply_cursor_uno_format(
    cursor: Any,
    uno_name: str,
    arguments: dict[str, Any],
    warnings: list[str] | None = None,
) -> bool:
    """Write a formatting ``.uno`` command onto *cursor*'s selection.

    Moving an independent ``XTextCursor`` and then only ``select``ing
    the shape paints Bold/Italic on every character: the dispatch
    sees the whole object. Draw applies those commands to the
    text-edit selection (``SdrBeginTextEdit`` +
    ``EditView::SetSelection`` in ``drviews2.cxx``). The model
    cursor is not that view selection. Writing the same properties
    on the cursor formats the range the sub-command selected.
    """
    pairs = _cursor_format_pairs(uno_name, arguments)
    if not pairs:
        return False
    applied_count = 0
    for name, value in pairs:
        if _set_text_prop(cursor, name, value):
            applied_count += 1
        else:
            if applied_count > 0 and warnings is not None:
                warnings.append("Property %s failed after partial format application" % name)
            return False
    return applied_count > 0


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
        raw_current = (
            tctx.active_page_index
            if getattr(tctx, "active_page_index", None) is not None
            else self.bridge.get_active_page_index()
        )
        try:
            count = int(self._page_count())
        except (TypeError, ValueError):
            count = 0
        try:
            raw_idx = int(raw_current) if raw_current is not None else 0
        except (TypeError, ValueError):
            raw_idx = 0
        self.current_slide = max(0, min(raw_idx, count - 1)) if count > 0 else 0
        self.applied: list[str] = []
        self.warnings: list[str] = []
        self._selection_mode: str = "none"

    def apply(self, transform_obj: dict[str, Any]) -> dict[str, Any]:
        try:
            # Compound undo groups transform operations without rollback.
            with WriterCompoundUndo(self.doc, "WriterAgent: Transform document structure"):
                top_uno = transform_obj.get("UnoCommand")
                if top_uno is not None:
                    # Top-level UnoCommand runs before SlideCommands.
                    self._apply_top_level_uno(top_uno)
                    self.current_slide = self.bridge.get_active_page_index()
                cmds = get_slide_commands(transform_obj)
                for idx, cmd in enumerate(cmds):
                    if not self._apply_command(cmd):
                        self.bridge.set_current_page_index(self.current_slide)
                        msg = self.warnings[-1] if self.warnings else "Command failed"
                        return {
                            "status": "error",
                            "message": msg,
                            "failed_at": idx,
                            "applied": self.applied,
                            "warnings": self.warnings,
                            "current_slide": self.current_slide,
                        }
                self.bridge.set_current_page_index(self.current_slide)
                return {
                    "status": "ok",
                    "current_slide": self.current_slide,
                    "applied": self.applied,
                    "warnings": self.warnings,
                }
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.exception("SlideCommandEngine.apply failed")
            return {
                "status": "error",
                "message": str(exc),
                "applied": self.applied,
                "warnings": self.warnings,
                "current_slide": self.current_slide,
            }

    def _page_count(self) -> int:
        return self.pages.getCount()

    def _current_page(self) -> Any:
        return self.pages.getByIndex(self.current_slide)

    def _apply_command(self, cmd: dict[str, Any]) -> bool:
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
                    return False
            elif key == "InsertMasterSlide":
                if isinstance(value, bool):
                    self.warnings.append("Invalid InsertMasterSlide index: %r" % value)
                    return False
                try:
                    master_idx = int(value)
                except (TypeError, ValueError):
                    self.warnings.append("Invalid InsertMasterSlide index: %r" % value)
                    return False
                if not self._insert_master(master_index=master_idx):
                    return False
            elif key == "InsertMasterSlideByName":
                if not self._insert_master(master_name=str(value)):
                    return False
            elif key == "DeleteSlide":
                self._delete_slide(value)
            elif key == "DuplicateSlide":
                self._duplicate_slide(value)
            elif key == "MoveSlide":
                if isinstance(value, bool):
                    self.warnings.append("Invalid MoveSlide index: %r" % value)
                    return False
                try:
                    to_idx = int(value)
                except (TypeError, ValueError):
                    self.warnings.append("Invalid MoveSlide index: %r" % value)
                    return False
                if not self._move_slide(self.current_slide, to_idx):
                    return False
            elif key == "RenameSlide":
                if self.bridge.rename_slide(self.current_slide, str(value)):
                    self.applied.append("RenameSlide:%s" % value)
                else:
                    self.warnings.append("RenameSlide failed")
            elif key in ("ChangeLayout", "ChangeLayoutByName"):
                if not self._set_layout(resolve_layout_id(value), key):
                    return False
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
                    from_idx = int(m.group(1))
                    if isinstance(value, bool):
                        self.warnings.append("Invalid MoveSlide index: %r" % value)
                        return False
                    try:
                        to_idx = int(value)
                    except (TypeError, ValueError):
                        self.warnings.append("Invalid MoveSlide index: %r" % value)
                        return False
                    if not self._move_slide(from_idx, to_idx):
                        return False
                    continue
                self.warnings.append("Unknown or unsupported command key: %s" % key)
        return True

    def _jump_to_name(self, name: str) -> int | None:
        for i in range(self._page_count()):
            page = self.pages.getByIndex(i)
            try:
                if hasattr(page, "Name") and page.Name == name:
                    self.current_slide = i
                    self.applied.append("JumpToSlideByName:%s" % name)
                    return i
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                # no-op after the dispose re-raise; non-dispose falls through
        return None

    def _insert_master(self, master_index: int | None = None, master_name: str | None = None) -> bool:
        try:
            _unused, new_idx = self.bridge.insert_slide_from_master(
                master_index=master_index,
                master_name=master_name,
                after_index=self.current_slide,
                switch=True,
            )
            self.current_slide = new_idx
            self.pages = self.bridge.get_pages()
            self.applied.append("InsertMasterSlide:%d" % new_idx)
            return True
        except Exception as e:
            if is_disposed_exception(e):
                raise
            self.warnings.append(f"InsertMasterSlide: {e}")
            return False

    def _delete_slide(self, val: Any) -> None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            self.warnings.append("Invalid DeleteSlide: %r (no such slide)" % val)
            return
        if self._page_count() <= 1:
            self.warnings.append("Cannot delete the only slide")
            return
        self.bridge.delete_slide(idx)
        self.pages = self.bridge.get_pages()
        self.current_slide = _current_after_delete(self.current_slide, idx, self._page_count())
        self.applied.append("DeleteSlide:%d" % idx)

    def _duplicate_slide(self, val: Any) -> None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            self.warnings.append("Invalid DuplicateSlide: %r (no such slide)" % val)
            return
        self.bridge.duplicate_slide(idx, switch=True)
        self.pages = self.bridge.get_pages()
        self.current_slide = min(idx + 1, self._page_count() - 1)
        self.applied.append("DuplicateSlide:%d" % idx)

    def _move_slide(self, from_idx: int, to_idx: int) -> bool:
        if self.bridge.move_slide(from_idx, to_idx):
            self.pages = self.bridge.get_pages()
            self.current_slide = _current_after_move(self.current_slide, from_idx, to_idx)
            self.applied.append("MoveSlide:%d->%d" % (from_idx, to_idx))
            return True
        else:
            self.warnings.append("MoveSlide failed %d -> %d" % (from_idx, to_idx))
            return False

    def _set_layout(self, layout_id: int | None, key: str) -> bool:
        if layout_id is None or layout_id not in _VALID_LAYOUT_IDS:
            self.warnings.append("Unknown layout in %s" % key)
            return False
        page = self._current_page()
        page.Layout = layout_id
        self.applied.append("%s:%d" % (key, layout_id))
        return True

    def _set_text_index(self, shape_index: int, text: str) -> None:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            self.warnings.append("SetText.%d: shape index out of range (have %d shapes)" % (shape_index, count))
            return
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            self.warnings.append("SetText.%d: shape has no callable getText" % shape_index)
            return
        if _set_shape_text(shape, text):
            self.applied.append("SetText.%d" % shape_index)
        else:
            self.warnings.append("SetText.%d: failed to set text" % shape_index)

    def _edit_text_object(self, shape_index: int, subcmds: list[Any]) -> None:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            self.warnings.append("EditTextObject.%d: shape index out of range" % shape_index)
            return
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            self.warnings.append("EditTextObject.%d: shape has no callable getText" % shape_index)
            return
        cursor = None
        xtext = None
        try:
            xtext = shape.getText()
            if xtext is not None and hasattr(xtext, "createTextCursor") and callable(getattr(xtext, "createTextCursor", None)):
                cursor = xtext.createTextCursor()
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            self.warnings.append("EditTextObject.%d: no text: %s" % (shape_index, exc))
            return
        if cursor is None or xtext is None:
            self.warnings.append("EditTextObject.%d: failed to create text cursor" % shape_index)
            return

        self._selection_mode = "none"
        warnings_before = len(self.warnings)
        success = True
        recognized_count = 0

        for sub in subcmds:
            if not isinstance(sub, dict):
                self.warnings.append("EditTextObject.%d: sub-command must be an object: %r" % (shape_index, sub))
                success = False
                continue
            for sk, sv in sub.items():
                if sk == "SelectText":
                    recognized_count += 1
                    cursor = self._select_text(xtext, cursor, sv)
                elif sk == "SelectParagraph":
                    recognized_count += 1
                    cursor = self._select_paragraph(xtext, cursor, sv)
                elif sk == "InsertText":
                    recognized_count += 1
                    try:
                        cursor.setString(str(sv))
                        self._selection_mode = "caret"
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        self.warnings.append("InsertText failed: %s" % exc)
                        success = False
                elif sk == "UnoCommand":
                    recognized_count += 1
                    if not self._dispatch_uno_string(sv, cursor=cursor, shape=shape, selection_mode=self._selection_mode):
                        success = False
                else:
                    self.warnings.append("Unknown EditTextObject sub-command: %s" % sk)
                    success = False

        if success and len(self.warnings) == warnings_before and recognized_count > 0:
            self.applied.append("EditTextObject.%d" % shape_index)

    def _select_text(self, xtext: Any, cursor: Any, spec: Any) -> Any:
        if cursor is None or xtext is None:
            return cursor
        if spec == [] or spec is None:
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            self._selection_mode = "all"
            return cursor
        if not isinstance(spec, list):
            self.warnings.append("SelectText requires a list or empty array: %r" % spec)
            return cursor
        int_spec: list[int] = []
        for item in spec:
            if isinstance(item, bool):
                self.warnings.append("Invalid non-int element in SelectText: %r" % item)
                return cursor
            try:
                int_spec.append(int(item))
            except (TypeError, ValueError):
                self.warnings.append("Invalid non-int element in SelectText: %r" % item)
                return cursor

        n = len(int_spec)
        if n == 1:
            para = int_spec[0]
            if para < 0:
                self.warnings.append("SelectText: negative paragraph index %d" % para)
                return cursor
            cursor.gotoStart(False)
            for _unused in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            self._selection_mode = "range"
            return cursor
        elif n == 2:
            para, char = int_spec[0], int_spec[1]
            if para < 0 or char < 0:
                self.warnings.append("SelectText: negative index [%d, %d]" % (para, char))
                return cursor
            cursor.gotoStart(False)
            for _unused in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(char, False)
            self._selection_mode = "caret"
            return cursor
        elif n == 3:
            para, char, end_para = int_spec[0], int_spec[1], int_spec[2]
            if para < 0 or char < 0 or end_para < 0 or end_para < para:
                self.warnings.append("SelectText: negative or invalid span [%d, %d, %d]" % (para, char, end_para))
                return cursor
            cursor.gotoStart(False)
            for _unused in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(char, False)
            for _unused in range(end_para - para):
                if not cursor.gotoNextParagraph(True):
                    break
            cursor.gotoEndOfParagraph(True)
            self._selection_mode = "range"
            return cursor
        elif n >= 4:
            para, start, end_para, end_char = int_spec[0], int_spec[1], int_spec[2], int_spec[3]
            if para < 0 or start < 0 or end_para < 0 or end_char < 0:
                self.warnings.append("SelectText: negative coordinates [%d, %d, %d, %d]" % (para, start, end_para, end_char))
                return cursor
            if end_para < para or (end_para == para and end_char < start):
                self.warnings.append("SelectText: negative span [%d, %d] to [%d, %d]" % (para, start, end_para, end_char))
                return cursor
            cursor.gotoStart(False)
            for _unused in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(start, False)
            if end_para == para:
                span = end_char - start
                cursor.goRight(span, True)
            else:
                for _unused in range(end_para - para):
                    if not cursor.gotoNextParagraph(True):
                        break
                cursor.goRight(end_char, True)
            self._selection_mode = "range"
            return cursor
        else:
            self.warnings.append("Invalid SelectText specification: %r" % spec)
            return cursor

    def _select_paragraph(self, xtext: Any, cursor: Any, para_index: Any) -> Any:
        if isinstance(para_index, bool):
            self.warnings.append("Invalid SelectParagraph index: %r" % para_index)
            return cursor
        try:
            val = int(para_index)
        except (TypeError, ValueError):
            self.warnings.append("Invalid SelectParagraph index: %r" % para_index)
            return cursor
        return self._select_text(xtext, cursor, [val])

    def _apply_top_level_uno(self, uno_spec: Any) -> None:
        if isinstance(uno_spec, dict):
            name = uno_spec.get("name") or uno_spec.get("Name")
            args = uno_spec.get("arguments") or uno_spec.get("Arguments") or {}
            if self._dispatch_uno_named(str(name), args):
                self.applied.append("UnoCommand:%s" % name)
        else:
            if self._dispatch_uno_string(uno_spec):
                self.applied.append("UnoCommand")

    def _dispatch_uno_string(self, cmd: Any, cursor: Any = None, shape: Any = None, selection_mode: str = "none") -> bool:
        if not isinstance(cmd, str):
            return False
        cmd = cmd.strip()
        if not cmd:
            return False
        # ".uno:Bold" or '.uno:Color {"Color.Color":...}'
        parts = cmd.split(None, 1)
        uno_name = parts[0]
        arg_json = parts[1] if len(parts) > 1 else None
        parsed: dict[str, Any] = {}
        if arg_json:
            from plugin.framework.json_utils import safe_json_loads

            loaded = safe_json_loads(arg_json, default={})
            if isinstance(loaded, dict):
                parsed = loaded
        if selection_mode == "caret":
            self.warnings.append("UnoCommand %s cannot be applied to a collapsed caret selection" % uno_name)
            return False
        # Range-scoped formatting must hit the SelectText cursor; selecting the
        # shape and dispatching would format every character.
        if selection_mode == "range" or _text_cursor_is_partial_selection(cursor, shape):
            if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                return True
            self.warnings.append("UnoCommand %s was not applied to the text selection" % uno_name)
            return False
        props = self._uno_props_from_dict(parsed) if parsed else ()
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                return False
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            if cursor is not None and shape is not None:
                try:
                    controller.select(shape)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    # no-op after the dispose re-raise; non-dispose falls through
            dispatcher.executeDispatch(frame, uno_name, "", 0, props)
            return True
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            self.warnings.append("UnoCommand %s failed: %s" % (uno_name, exc))
            return False

    def _dispatch_uno_named(self, name: str, arguments: dict[str, Any]) -> bool:
        props = self._uno_props_from_dict(arguments)
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                return False
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            dispatcher.executeDispatch(frame, name, "", 0, props)
            return True
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            self.warnings.append("UnoCommand %s failed: %s" % (name, exc))
            return False

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
