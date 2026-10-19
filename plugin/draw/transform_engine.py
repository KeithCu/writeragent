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
from plugin.framework.errors import is_disposed_exception, uno_probe
from plugin.writer.edit_review import WriterCompoundUndo

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_SET_TEXT_RE = re.compile(r"^SetText\.(\d+)$", re.I)
_EDIT_TEXT_RE = re.compile(r"^EditTextObject\.(\d+)$", re.I)
_MOVE_SLIDE_RE = re.compile(r"^MoveSlide\.(\d+)$", re.I)
_VALID_LAYOUT_IDS = frozenset(AUTOLAYOUT_ID.values())


def _clear_empty_pres_obj(shape: Any) -> None:
    def _apply() -> None:
        if hasattr(shape, "IsEmptyPresentationObject"):
            setattr(shape, "IsEmptyPresentationObject", False)
        elif hasattr(shape, "setPropertyValue") and callable(getattr(shape, "setPropertyValue", None)):
            shape.setPropertyValue("IsEmptyPresentationObject", False)

    uno_probe(_apply, None)



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


def _text_cursor_is_partial_selection(cursor: Any, shape: Any) -> bool | None:
    """True when *cursor* covers a range shorter than the whole shape, None if read raises.

    An empty ``createTextCursor()`` is not a range: ``.uno:Bold`` still
    formats the shape. ``SelectText: []`` covers the whole string, which
    is the same as selecting the shape.
    """
    if cursor is None:
        return False
    getter = getattr(cursor, "getString", None)
    if callable(getter):
        try:
            selected = getter()
        except Exception as e:
            if is_disposed_exception(e):
                raise
            return None
        if not isinstance(selected, str):
            selected = None
    elif hasattr(cursor, "String"):
        val = getattr(cursor, "String", None)
        selected = val if isinstance(val, str) else None
    else:
        selected = None

    if not selected:
        return False

    try:
        full = _shape_text_string(shape)
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return None
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


def _calculate_text_selection_span(text: str, int_spec: list[int]) -> tuple[int, int, str]:
    """Calculate half-open character span (start, end, mode) from paragraph/char spec.

    Tip: Impress shape text cursors can lack XParagraphCursor, so select by
    character offsets with plain XTextCursor moves.
    """
    paras = text.replace("\r\n", "\n").split("\n")
    num_paras = len(paras)

    def _para_start(p_idx: int) -> int:
        return sum(len(paras[i]) + 1 for i in range(p_idx))

    n = len(int_spec)
    if n == 1:
        p = min(int_spec[0], num_paras - 1)
        start = _para_start(p)
        end = start + len(paras[p])
        return start, end, "range"
    elif n == 2:
        p = min(int_spec[0], num_paras - 1)
        start = _para_start(p) + int_spec[1]
        return start, start, "caret"
    elif n == 3:
        p = min(int_spec[0], num_paras - 1)
        ep = min(int_spec[2], num_paras - 1)
        start = _para_start(p) + int_spec[1]
        end = _para_start(ep) + len(paras[ep])
        return start, end, "range"
    else:
        p = min(int_spec[0], num_paras - 1)
        ep = min(int_spec[2], num_paras - 1)
        start = _para_start(p) + int_spec[1]
        end = _para_start(ep) + int_spec[3]
        return start, end, "range"


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

    def _ok_result(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "current_slide": self.current_slide,
            "applied": self.applied,
            "warnings": self.warnings,
        }

    def _error_result(self, message: str, failed_at: int | None = None) -> dict[str, Any]:
        res: dict[str, Any] = {
            "status": "error",
            "message": message,
            "applied": self.applied,
            "warnings": self.warnings,
            "current_slide": self.current_slide,
        }
        if failed_at is not None:
            res["failed_at"] = failed_at
        return res

    def apply(self, transform_obj: dict[str, Any]) -> dict[str, Any]:
        try:
            # Compound undo groups transform operations without rollback.
            with WriterCompoundUndo(self.doc, "WriterAgent: Transform document structure"):
                top_uno = transform_obj.get("UnoCommand")
                if top_uno is not None:
                    # Top-level UnoCommand runs before SlideCommands.
                    if not self._apply_top_level_uno(top_uno):
                        msg = self.warnings[-1] if self.warnings else "UnoCommand failed"
                        return self._error_result(message=msg)
                    # Only sync active page when controller has a current page (headless/hidden doc falls back to 0).
                    ctrl = getattr(self.doc, "getCurrentController", None)
                    controller = ctrl() if callable(ctrl) else None
                    get_page = getattr(controller, "getCurrentPage", None) if controller is not None else None
                    if callable(get_page) and get_page() is not None:
                        self.current_slide = self.bridge.get_active_page_index()
                cmds = get_slide_commands(transform_obj)
                for idx, cmd in enumerate(cmds):
                    try:
                        if not self._apply_command(cmd):
                            self.bridge.set_current_page_index(self.current_slide)
                            msg = self.warnings[-1] if self.warnings else "Command failed"
                            return self._error_result(message=msg, failed_at=idx)
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        cmd_key = next(iter(cmd.keys())) if isinstance(cmd, dict) and cmd else "Command"
                        self.warnings.append(f"{cmd_key} failed: {exc}")
                        self.bridge.set_current_page_index(self.current_slide)
                        return self._error_result(message=self.warnings[-1], failed_at=idx)
                self.bridge.set_current_page_index(self.current_slide)
                return self._ok_result()
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.exception("SlideCommandEngine.apply failed")
            return self._error_result(message=str(exc))

    def _page_count(self) -> int:
        return self.pages.getCount()

    def _current_page(self) -> Any:
        return self.pages.getByIndex(self.current_slide)

    def _apply_command(self, cmd: dict[str, Any]) -> bool:
        for key, value in cmd.items():
            if is_deferred_command_key(key):
                self.warnings.append("%s is not supported in WriterAgent V1; use image_generate or atomic draw tools." % key)
                return False
            if key == "JumpToSlide":
                idx = _parse_slide_index(value, self.current_slide, self._page_count())
                if idx is not None:
                    self.current_slide = idx
                    self.applied.append("JumpToSlide:%d" % idx)
                else:
                    self.warnings.append("Invalid JumpToSlide: %r" % value)
                    return False
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
                if not self._delete_slide(value):
                    return False
            elif key == "DuplicateSlide":
                if not self._duplicate_slide(value):
                    return False
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
                    return False
            elif key in ("ChangeLayout", "ChangeLayoutByName"):
                if not self._set_layout(resolve_layout_id(value), key):
                    return False
            elif key == "UnoCommand":
                warnings_before = len(self.warnings)
                if self._dispatch_uno_string(value):
                    self.applied.append("UnoCommand:%s" % value)
                else:
                    if len(self.warnings) == warnings_before:
                        self.warnings.append("UnoCommand %s failed" % value)
                    return False
            else:
                m = _SET_TEXT_RE.match(key)
                if m:
                    if not self._set_text_index(int(m.group(1)), str(value)):
                        return False
                    continue
                m = _EDIT_TEXT_RE.match(key)
                if m:
                    if isinstance(value, list):
                        if not self._edit_text_object(int(m.group(1)), value):
                            return False
                    else:
                        self.warnings.append("%s requires an array of sub-commands" % key)
                        return False
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
                return False
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

    def _delete_slide(self, val: Any) -> bool:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            self.warnings.append("Invalid DeleteSlide: %r (no such slide)" % val)
            return False
        if self._page_count() <= 1:
            self.warnings.append("Cannot delete the only slide")
            return False
        self.bridge.delete_slide(idx)
        self.pages = self.bridge.get_pages()
        self.current_slide = _current_after_delete(self.current_slide, idx, self._page_count())
        self.applied.append("DeleteSlide:%d" % idx)
        return True

    def _duplicate_slide(self, val: Any) -> bool:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            self.warnings.append("Invalid DuplicateSlide: %r (no such slide)" % val)
            return False
        try:
            self.bridge.duplicate_slide(idx, switch=True)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            self.warnings.append("DuplicateSlide failed: %s" % exc)
            return False
        self.pages = self.bridge.get_pages()
        self.current_slide = min(idx + 1, self._page_count() - 1)
        self.applied.append("DuplicateSlide:%d" % idx)
        return True

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
        # Upstream falls back to AUTOLAYOUT_TITLE_CONTENT on unknown layouts;
        # WriterAgent errors on unrecognized ids. Notes and handout ids remain valid.
        if layout_id is None or layout_id not in _VALID_LAYOUT_IDS:
            self.warnings.append("Unknown layout in %s" % key)
            return False
        page = self._current_page()
        page.Layout = layout_id
        self.applied.append("%s:%d" % (key, layout_id))
        return True

    def _set_text_index(self, shape_index: int, text: str) -> bool:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            self.warnings.append("SetText.%d: shape index out of range (have %d shapes)" % (shape_index, count))
            return False
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            self.warnings.append("SetText.%d: shape has no callable getText" % shape_index)
            return False
        if _set_shape_text(shape, text):
            _clear_empty_pres_obj(shape)
            self.applied.append("SetText.%d" % shape_index)
            return True
        else:
            self.warnings.append("SetText.%d: failed to set text" % shape_index)
            return False

    def _edit_text_object(self, shape_index: int, subcmds: list[Any]) -> bool:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            self.warnings.append("EditTextObject.%d: shape index out of range" % shape_index)
            return False
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            self.warnings.append("EditTextObject.%d: shape has no callable getText" % shape_index)
            return False
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
            return False
        if cursor is None or xtext is None:
            self.warnings.append("EditTextObject.%d: failed to create text cursor" % shape_index)
            return False

        selection_mode: str = "none"
        recognized_count = 0

        for sub in subcmds:
            if not isinstance(sub, dict):
                self.warnings.append("EditTextObject.%d: sub-command must be an object: %r" % (shape_index, sub))
                return False
            for sk, sv in sub.items():
                if sk == "SelectText":
                    recognized_count += 1
                    cursor, mode = self._select_text(xtext, cursor, sv)
                    if mode is None:
                        # Tip: Stop immediately on failed SelectText. Previously it left the old selection mode,
                        # causing subsequent UnoCommands to format the previous range while still failing overall.
                        return False
                    selection_mode = mode
                elif sk == "SelectParagraph":
                    recognized_count += 1
                    cursor, mode = self._select_paragraph(xtext, cursor, sv)
                    if mode is None:
                        return False
                    selection_mode = mode
                elif sk == "InsertText":
                    recognized_count += 1
                    inserted = str(sv)
                    try:
                        cursor.setString(inserted)
                        _clear_empty_pres_obj(shape)
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        self.warnings.append("InsertText failed: %s" % exc)
                        return False
                    # Upstream FuTransformDocumentStructure (drviews2.cxx) leaves inserted text selected.
                    if not inserted:
                        selection_mode = "caret"
                    else:
                        cur_str = _cursor_string(cursor)
                        if cur_str == inserted:
                            selection_mode = "range"
                        elif cur_str == "":
                            go_left = getattr(cursor, "goLeft", None)
                            if callable(go_left):
                                try:
                                    go_left(len(inserted), True)
                                except Exception as exc:
                                    if is_disposed_exception(exc):
                                        raise
                            if _cursor_string(cursor) == inserted:
                                selection_mode = "range"
                            else:
                                self.warnings.append("InsertText could not select inserted text")
                                return False
                        else:
                            self.warnings.append("InsertText could not select inserted text")
                            return False
                elif sk == "UnoCommand":
                    recognized_count += 1
                    if not self._dispatch_uno_string(sv, cursor=cursor, shape=shape, selection_mode=selection_mode):
                        return False
                else:
                    self.warnings.append("Unknown EditTextObject sub-command: %s" % sk)
                    return False

        if recognized_count > 0:
            self.applied.append("EditTextObject.%d" % shape_index)
            return True
        return False

    def _select_text(self, xtext: Any, cursor: Any, spec: Any) -> tuple[Any, str | None]:
        if cursor is None or xtext is None:
            return cursor, None
        if spec == [] or spec is None:
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor, "all"
        if not isinstance(spec, list):
            self.warnings.append("SelectText requires a list or empty array: %r" % spec)
            return cursor, None
        int_spec: list[int] = []
        for item in spec:
            if isinstance(item, bool):
                self.warnings.append("Invalid non-int element in SelectText: %r" % item)
                return cursor, None
            try:
                int_spec.append(int(item))
            except (TypeError, ValueError):
                self.warnings.append("Invalid non-int element in SelectText: %r" % item)
                return cursor, None

        n = len(int_spec)
        if n == 0:
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor, "all"
        if n == 1:
            para = int_spec[0]
            if para < 0:
                self.warnings.append("SelectText: negative paragraph index %d" % para)
                return cursor, None
        elif n == 2:
            para, char = int_spec[0], int_spec[1]
            if para < 0 or char < 0:
                self.warnings.append("SelectText: negative index [%d, %d]" % (para, char))
                return cursor, None
        elif n == 3:
            para, char, end_para = int_spec[0], int_spec[1], int_spec[2]
            if para < 0 or char < 0 or end_para < 0 or end_para < para:
                self.warnings.append("SelectText: negative or invalid span [%d, %d, %d]" % (para, char, end_para))
                return cursor, None
        elif n >= 4:
            para, start, end_para, end_char = int_spec[0], int_spec[1], int_spec[2], int_spec[3]
            if para < 0 or start < 0 or end_para < 0 or end_char < 0:
                self.warnings.append("SelectText: negative coordinates [%d, %d, %d, %d]" % (para, start, end_para, end_char))
                return cursor, None
            if end_para < para or (end_para == para and end_char < start):
                self.warnings.append("SelectText: negative span [%d, %d] to [%d, %d]" % (para, start, end_para, end_char))
                return cursor, None

        raw_text = ""
        getter = getattr(xtext, "getString", None)
        if callable(getter):
            try:
                val = getter()
                if isinstance(val, str):
                    raw_text = val
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                raw_text = ""
        elif hasattr(xtext, "String"):
            val = getattr(xtext, "String", None)
            if isinstance(val, str):
                raw_text = val

        start, end, mode = _calculate_text_selection_span(raw_text, int_spec)

        cursor.gotoStart(False)
        if start > 0:
            cursor.goRight(start, False)
        if mode == "range":
            span = max(0, end - start)
            cursor.goRight(span, True)
        return cursor, mode

    def _select_paragraph(self, xtext: Any, cursor: Any, para_index: Any) -> tuple[Any, str | None]:
        if isinstance(para_index, bool):
            self.warnings.append("Invalid SelectParagraph index: %r" % para_index)
            return cursor, None
        try:
            val = int(para_index)
        except (TypeError, ValueError):
            self.warnings.append("Invalid SelectParagraph index: %r" % para_index)
            return cursor, None
        return self._select_text(xtext, cursor, [val])

    def _apply_top_level_uno(self, uno_spec: Any) -> bool:
        warnings_before = len(self.warnings)
        success = False
        name = ""
        if isinstance(uno_spec, dict):
            name = str(uno_spec.get("name") or uno_spec.get("Name") or "")
            args = uno_spec.get("arguments") or uno_spec.get("Arguments") or {}
            if name and self._dispatch_uno_named(name, args):
                self.applied.append("UnoCommand:%s" % name)
                success = True
        elif isinstance(uno_spec, str):
            name = uno_spec
            if self._dispatch_uno_string(uno_spec):
                self.applied.append("UnoCommand")
                success = True
        else:
            self.warnings.append("Invalid UnoCommand specification: %r" % uno_spec)
            return False

        if not success:
            if len(self.warnings) == warnings_before:
                self.warnings.append("UnoCommand %s failed" % name)
            return False
        return True

    def _execute_uno(self, name: str, props: tuple[Any, ...]) -> bool:
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                self.warnings.append("UnoCommand %s failed: document has no active controller" % name)
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

            loaded = safe_json_loads(arg_json, default=None)
            if not isinstance(loaded, dict):
                self.warnings.append("UnoCommand %s has invalid JSON arguments: %r" % (uno_name, arg_json))
                return False
            parsed = loaded
        if selection_mode == "caret":
            self.warnings.append("UnoCommand %s cannot be applied to a collapsed caret selection" % uno_name)
            return False
        # Range-scoped formatting must hit the SelectText cursor; selecting the
        # shape and dispatching would format every character.
        if selection_mode == "range":
            if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                return True
            self.warnings.append("UnoCommand %s was not applied to the text selection" % uno_name)
            return False
        if selection_mode == "none":
            is_partial = _text_cursor_is_partial_selection(cursor, shape)
            if is_partial is None:
                self.warnings.append("UnoCommand %s failed to read text cursor" % uno_name)
                return False
            if is_partial:
                if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                    return True
                self.warnings.append("UnoCommand %s was not applied to the text selection" % uno_name)
                return False
        props = self._uno_props_from_dict(parsed) if parsed else ()
        if cursor is not None and shape is not None:
            try:
                controller = self.doc.getCurrentController()
                if controller is not None:
                    controller.select(shape)
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                # no-op after the dispose re-raise; non-dispose falls through
        return self._execute_uno(uno_name, props)

    def _dispatch_uno_named(self, name: str, arguments: dict[str, Any]) -> bool:
        props = self._uno_props_from_dict(arguments) if arguments else ()
        return self._execute_uno(name, props)

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
