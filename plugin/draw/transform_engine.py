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
from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import normalize_linebreaks
from plugin.draw.bridge import DrawBridge
from plugin.draw.transform_schema import (
    EDIT_TEXT_RE,
    MOVE_SLIDE_RE,
    SET_TEXT_RE,
    coerce_int,
    get_slide_commands,
    is_deferred_command_key,
    parse_slide_index_token,
    resolve_layout_id,
    split_uno_command,
    validate_slide_commands,
    validate_top_level_uno,
)
from plugin.framework.errors import is_disposed_exception, uno_probe
from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.draw.shapes import set_shape_text

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

def _clear_empty_pres_obj(shape: Any) -> None:
    def _apply() -> None:
        if hasattr(shape, "IsEmptyPresentationObject"):
            setattr(shape, "IsEmptyPresentationObject", False)
        elif hasattr(shape, "setPropertyValue") and callable(getattr(shape, "setPropertyValue", None)):
            shape.setPropertyValue("IsEmptyPresentationObject", False)

    uno_probe(_apply, None)



def _parse_slide_index(val: Any, current: int, page_count: int, *, clamp: bool = True) -> int | None:
    """Slide index from "", "last", or a number. None when it is not valid.

    clamp=False rejects an out-of-range number. DeleteSlide and DuplicateSlide
    use that: clamping made {"DeleteSlide": 99} delete the last slide.
    """
    token = parse_slide_index_token(val)
    if token is None:
        return None
    kind, idx = token
    if kind == "current":
        return current
    if kind == "last":
        return max(0, page_count - 1)
    assert idx is not None
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


def _calculate_text_selection_span(text: str, int_spec: list[int]) -> tuple[int, int, str] | str:
    """Calculate half-open character span (start, end, mode) or error message.

    Tip: drviews2.cxx SelectText normalizes oversized indexes with EE_TEXTPOS_MAX,
    but clamping would format text the command did not name. Error instead.
    """
    paras = normalize_linebreaks(text).split("\n")
    num_paras = len(paras)

    def _para_start(p_idx: int) -> int:
        return sum(len(paras[i]) + 1 for i in range(p_idx))

    n = len(int_spec)
    if n == 1:
        p = int_spec[0]
        if p >= num_paras:
            return "SelectText: paragraph index %d out of range (have %d)" % (p, num_paras)
        start = _para_start(p)
        end = start + len(paras[p])
        return start, end, "range"
    elif n == 2:
        p, c = int_spec[0], int_spec[1]
        if p >= num_paras:
            return "SelectText: paragraph index %d out of range (have %d)" % (p, num_paras)
        if c > len(paras[p]):
            return "SelectText: char index %d past paragraph %d length %d" % (c, p, len(paras[p]))
        start = _para_start(p) + c
        return start, start, "caret"
    elif n == 3:
        p, c, ep = int_spec[0], int_spec[1], int_spec[2]
        if p >= num_paras:
            return "SelectText: paragraph index %d out of range (have %d)" % (p, num_paras)
        if ep >= num_paras:
            return "SelectText: end paragraph index %d out of range (have %d)" % (ep, num_paras)
        if ep < p:
            return "SelectText: negative or invalid span [%d, %d, %d]" % (p, c, ep)
        if c > len(paras[p]):
            return "SelectText: char index %d past paragraph %d length %d" % (c, p, len(paras[p]))
        start = _para_start(p) + c
        end = _para_start(ep) + len(paras[ep])
        return start, end, "range"
    elif n >= 4:
        p, start_c, ep, end_c = int_spec[0], int_spec[1], int_spec[2], int_spec[3]
        if p >= num_paras:
            return "SelectText: paragraph index %d out of range (have %d)" % (p, num_paras)
        if ep >= num_paras:
            return "SelectText: end paragraph index %d out of range (have %d)" % (ep, num_paras)
        if ep < p or (ep == p and end_c < start_c):
            return "SelectText: negative span [%d, %d] to [%d, %d]" % (p, start_c, ep, end_c)
        if start_c > len(paras[p]):
            return "SelectText: char index %d past paragraph %d length %d" % (start_c, p, len(paras[p]))
        if end_c > len(paras[ep]):
            return "SelectText: char index %d past paragraph %d length %d" % (end_c, ep, len(paras[ep]))
        start = _para_start(p) + start_c
        end = _para_start(ep) + end_c
        return start, end, "range"
    return "SelectText: empty specification"



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

    def _fail(self, msg: str) -> str:
        self.warnings.append(msg)
        return msg

    def apply(self, transform_obj: dict[str, Any]) -> dict[str, Any]:
        val_err, failed_at = validate_slide_commands(transform_obj)
        if val_err is not None:
            return self._error_result(message=val_err, failed_at=failed_at)
        if "UnoCommand" in transform_obj:
            top_err = validate_top_level_uno(transform_obj["UnoCommand"])
            if top_err is not None:
                return self._error_result(message=top_err)
        try:
            # Compound undo groups transform operations without rollback.
            with WriterCompoundUndo(self.doc, "WriterAgent: Transform document structure"):
                top_uno = transform_obj.get("UnoCommand")
                if top_uno is not None:
                    # Top-level UnoCommand runs before SlideCommands.
                    top_err = self._apply_top_level_uno(top_uno)
                    if top_err is not None and top_err is not True:
                        return self._error_result(message=top_err if isinstance(top_err, str) else "UnoCommand failed")
                    # Only sync active page when controller has a current page (headless/hidden doc falls back to 0).
                    ctrl = getattr(self.doc, "getCurrentController", None)
                    controller = ctrl() if callable(ctrl) else None
                    get_page = getattr(controller, "getCurrentPage", None) if controller is not None else None
                    if callable(get_page) and get_page() is not None:
                        self.current_slide = self.bridge.get_active_page_index()
                cmds = get_slide_commands(transform_obj)
                for idx, cmd in enumerate(cmds):
                    try:
                        err_msg = self._apply_command(cmd)
                        if err_msg is not None:
                            self.bridge.set_current_page_index(self.current_slide)
                            return self._error_result(message=err_msg, failed_at=idx)
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        cmd_key = next(iter(cmd.keys())) if isinstance(cmd, dict) and cmd else "Command"
                        msg = f"{cmd_key} failed: {exc}"
                        self.warnings.append(msg)
                        self.bridge.set_current_page_index(self.current_slide)
                        return self._error_result(message=msg, failed_at=idx)
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

    def _apply_command(self, cmd: dict[str, Any]) -> str | None:
        for key, value in cmd.items():
            if is_deferred_command_key(key):
                return self._fail("%s is not supported in WriterAgent V1; use image_generate or atomic draw tools." % key)
            if key == "JumpToSlide":
                idx = _parse_slide_index(value, self.current_slide, self._page_count())
                if idx is not None:
                    self.current_slide = idx
                    self.applied.append("JumpToSlide:%d" % idx)
                else:
                    return self._fail("Invalid JumpToSlide: %r" % value)
            elif key == "JumpToSlideByName":
                if not isinstance(value, str):
                    return self._fail("%s requires a string" % key)
                found = self._jump_to_name(value)
                if found is None:
                    return self._fail("JumpToSlideByName: slide not found: %r" % value)
            elif key == "InsertMasterSlide":
                master_idx = coerce_int(value)
                if master_idx is None:
                    return self._fail("Invalid InsertMasterSlide index: %r" % value)
                err = self._insert_master(master_index=master_idx)
                if err is not None:
                    return err
            elif key == "InsertMasterSlideByName":
                if not isinstance(value, str):
                    return self._fail("%s requires a string" % key)
                err = self._insert_master(master_name=value)
                if err is not None:
                    return err
            elif key == "DeleteSlide":
                err = self._delete_slide(value)
                if err is not None:
                    return err
            elif key == "DuplicateSlide":
                err = self._duplicate_slide(value)
                if err is not None:
                    return err
            elif key == "MoveSlide":
                to_idx = coerce_int(value)
                if to_idx is None:
                    return self._fail("Invalid MoveSlide index: %r" % value)
                err = self._move_slide(self.current_slide, to_idx)
                if err is not None:
                    return err
            elif key == "RenameSlide":
                if not isinstance(value, str):
                    return self._fail("%s requires a string" % key)
                if self.bridge.rename_slide(self.current_slide, value):
                    self.applied.append("RenameSlide:%s" % value)
                else:
                    return self._fail("RenameSlide failed")
            elif key in ("ChangeLayout", "ChangeLayoutByName"):
                err = self._set_layout(resolve_layout_id(value), key)
                if err is not None:
                    return err
            elif key == "UnoCommand":
                err = self._dispatch_uno_string(value)
                if err is not None:
                    return err
                self.applied.append("UnoCommand:%s" % value)
            else:
                m = SET_TEXT_RE.match(key)
                if m:
                    err = self._set_text_index(int(m.group(1)), str(value))
                    if err is not None:
                        return err
                    continue
                m = EDIT_TEXT_RE.match(key)
                if m:
                    if isinstance(value, list):
                        err = self._edit_text_object(int(m.group(1)), value)
                        if err is not None:
                            return err
                    else:
                        return self._fail("%s requires an array of sub-commands" % key)
                    continue
                m = MOVE_SLIDE_RE.match(key)
                if m:
                    from_idx = int(m.group(1))
                    to_idx = coerce_int(value)
                    if to_idx is None:
                        return self._fail("Invalid MoveSlide index: %r" % value)
                    err = self._move_slide(from_idx, to_idx)
                    if err is not None:
                        return err
                    continue
                return self._fail("Unknown or unsupported command key: %s" % key)
        return None

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

    def _insert_master(self, master_index: int | None = None, master_name: str | None = None) -> str | None:
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
            return None
        except Exception as e:
            if is_disposed_exception(e):
                raise
            return self._fail(f"InsertMasterSlide: {e}")

    def _delete_slide(self, val: Any) -> str | None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            return self._fail("Invalid DeleteSlide: %r (no such slide)" % val)
        if self._page_count() <= 1:
            return self._fail("Cannot delete the only slide")
        self.bridge.delete_slide(idx)
        self.pages = self.bridge.get_pages()
        self.current_slide = _current_after_delete(self.current_slide, idx, self._page_count())
        self.applied.append("DeleteSlide:%d" % idx)
        return None

    def _duplicate_slide(self, val: Any) -> str | None:
        idx = _parse_slide_index(val, self.current_slide, self._page_count(), clamp=False)
        if idx is None:
            return self._fail("Invalid DuplicateSlide: %r (no such slide)" % val)
        try:
            self.bridge.duplicate_slide(idx, switch=True)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return self._fail("DuplicateSlide failed: %s" % exc)
        self.pages = self.bridge.get_pages()
        self.current_slide = min(idx + 1, self._page_count() - 1)
        self.applied.append("DuplicateSlide:%d" % idx)
        return None

    def _move_slide(self, from_idx: int, to_idx: int) -> str | None:
        if self.bridge.move_slide(from_idx, to_idx):
            self.pages = self.bridge.get_pages()
            self.current_slide = _current_after_move(self.current_slide, from_idx, to_idx)
            self.applied.append("MoveSlide:%d->%d" % (from_idx, to_idx))
            return None
        else:
            return self._fail("MoveSlide failed %d -> %d" % (from_idx, to_idx))

    def _set_layout(self, layout_id: int | None, key: str) -> str | None:
        # Unknown/invalid layout ids are rejected by validate_slide_commands before this runs.
        if layout_id is None:
            return self._fail("Unknown layout in %s" % key)
        page = self._current_page()
        page.Layout = layout_id
        self.applied.append("%s:%d" % (key, layout_id))
        return None

    def _set_text_index(self, shape_index: int, text: str) -> str | None:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            return self._fail("SetText.%d: shape index out of range (have %d shapes)" % (shape_index, count))
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            return self._fail("SetText.%d: shape has no callable getText" % shape_index)
        if set_shape_text(shape, text):
            _clear_empty_pres_obj(shape)
            self.applied.append("SetText.%d" % shape_index)
            return None
        else:
            return self._fail("SetText.%d: failed to set text" % shape_index)

    def _edit_text_object(self, shape_index: int, subcmds: list[Any]) -> str | None:
        page = self._current_page()
        count = page.getCount() if hasattr(page, "getCount") and callable(getattr(page, "getCount", None)) else 0
        if shape_index < 0 or shape_index >= count:
            return self._fail("EditTextObject.%d: shape index out of range" % shape_index)
        shape = page.getByIndex(shape_index)
        if not hasattr(shape, "getText") or not callable(getattr(shape, "getText", None)):
            return self._fail("EditTextObject.%d: shape has no callable getText" % shape_index)
        cursor = None
        xtext = None
        try:
            xtext = shape.getText()
            if xtext is not None and hasattr(xtext, "createTextCursor") and callable(getattr(xtext, "createTextCursor", None)):
                cursor = xtext.createTextCursor()
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return self._fail("EditTextObject.%d: no text: %s" % (shape_index, exc))
        if cursor is None or xtext is None:
            return self._fail("EditTextObject.%d: failed to create text cursor" % shape_index)

        if not subcmds:
            return self._fail("EditTextObject.%d: sub-commands list cannot be empty" % shape_index)

        selection_mode: str = "none"
        recognized_count = 0

        for sub in subcmds:
            if not isinstance(sub, dict):
                return self._fail("EditTextObject.%d: sub-command must be an object: %r" % (shape_index, sub))
            for sk, sv in sub.items():
                if sk == "SelectText":
                    recognized_count += 1
                    cursor, mode, err = self._select_text(xtext, cursor, sv)
                    if err is not None:
                        # Tip: Stop immediately on failed SelectText. Previously it left the old selection mode,
                        # causing subsequent UnoCommands to format the previous range while still failing overall.
                        return err
                    selection_mode = mode or "none"
                elif sk == "SelectParagraph":
                    recognized_count += 1
                    cursor, mode, err = self._select_paragraph(xtext, cursor, sv)
                    if err is not None:
                        return err
                    selection_mode = mode or "none"
                elif sk == "InsertText":
                    recognized_count += 1
                    inserted = normalize_linebreaks(str(sv))
                    try:
                        cursor.setString(inserted)
                        _clear_empty_pres_obj(shape)
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            raise
                        return self._fail("InsertText failed: %s" % exc)
                    # Upstream FuTransformDocumentStructure (drviews2.cxx) leaves inserted text selected.
                    if not inserted:
                        selection_mode = "caret"
                    else:
                        cur_str = _cursor_string(cursor)
                        cur_norm = normalize_linebreaks(cur_str) if isinstance(cur_str, str) else None
                        if cur_norm == inserted:
                            selection_mode = "range"
                        elif cur_str == "":
                            go_left = getattr(cursor, "goLeft", None)
                            if callable(go_left):
                                try:
                                    go_left(len(inserted), True)
                                except Exception as exc:
                                    if is_disposed_exception(exc):
                                        raise
                            left_str = _cursor_string(cursor)
                            left_norm = normalize_linebreaks(left_str) if isinstance(left_str, str) else None
                            if left_norm == inserted:
                                selection_mode = "range"
                            else:
                                return self._fail("InsertText could not select inserted text")
                        else:
                            return self._fail("InsertText could not select inserted text")
                elif sk == "UnoCommand":
                    recognized_count += 1
                    err = self._dispatch_uno_string(sv, cursor=cursor, shape=shape, selection_mode=selection_mode)
                    if err is not None:
                        return err
                else:
                    return self._fail("Unknown EditTextObject sub-command: %s" % sk)

        if recognized_count > 0:
            self.applied.append("EditTextObject.%d" % shape_index)
            return None
        return self._fail("EditTextObject.%d: sub-commands list cannot be empty" % shape_index)

    def _select_text(self, xtext: Any, cursor: Any, spec: Any) -> tuple[Any, str | None, str | None]:
        if cursor is None or xtext is None:
            return cursor, None, self._fail("SelectText: cursor or text is missing")
        if spec == [] or spec is None:
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor, "all", None
        if not isinstance(spec, list):
            return cursor, None, self._fail("SelectText requires a list or empty array: %r" % spec)
        int_spec: list[int] = []
        for item in spec:
            coerced = coerce_int(item)
            if coerced is None:
                return cursor, None, self._fail("Invalid non-int element in SelectText: %r" % item)
            int_spec.append(coerced)

        n = len(int_spec)
        if n == 0:
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor, "all", None
        if n == 1:
            para = int_spec[0]
            if para < 0:
                return cursor, None, self._fail("SelectText: negative paragraph index %d" % para)
        elif n == 2:
            para, char = int_spec[0], int_spec[1]
            if para < 0 or char < 0:
                return cursor, None, self._fail("SelectText: negative index [%d, %d]" % (para, char))
        elif n == 3:
            para, char, end_para = int_spec[0], int_spec[1], int_spec[2]
            if para < 0 or char < 0 or end_para < 0 or end_para < para:
                return cursor, None, self._fail("SelectText: negative or invalid span [%d, %d, %d]" % (para, char, end_para))
        elif n >= 4:
            para, start, end_para, end_char = int_spec[0], int_spec[1], int_spec[2], int_spec[3]
            if para < 0 or start < 0 or end_para < 0 or end_char < 0:
                return cursor, None, self._fail("SelectText: negative coordinates [%d, %d, %d, %d]" % (para, start, end_para, end_char))
            if end_para < para or (end_para == para and end_char < start):
                return cursor, None, self._fail("SelectText: negative span [%d, %d] to [%d, %d]" % (para, start, end_para, end_char))

        raw_text = _cursor_string(xtext)
        if raw_text is None:
            return cursor, None, self._fail("SelectText: failed to read text from shape")

        span_res = _calculate_text_selection_span(raw_text, int_spec)
        if isinstance(span_res, str):
            return cursor, None, self._fail(span_res)
        start, end, mode = span_res

        cursor.gotoStart(False)
        if start > 0:
            cursor.goRight(start, False)
        if mode == "range":
            span = max(0, end - start)
            cursor.goRight(span, True)
            return cursor, mode, None
        if mode == "caret":
            # Impress TextCursor.getString() can still return the whole shape for a
            # collapsed caret; collapse explicitly so formatting paths see a caret.
            collapse = getattr(cursor, "collapseToStart", None) or getattr(cursor, "collapseToEnd", None)
            if callable(collapse):
                try:
                    collapse()
                except Exception as exc:
                    if is_disposed_exception(exc):
                        raise
        return cursor, mode, None

    def _select_paragraph(self, xtext: Any, cursor: Any, para_index: Any) -> tuple[Any, str | None, str | None]:
        val = coerce_int(para_index)
        if val is None:
            return cursor, None, self._fail("Invalid SelectParagraph index: %r" % para_index)
        return self._select_text(xtext, cursor, [val])

    def _apply_top_level_uno(self, uno_spec: Any) -> str | None:
        if isinstance(uno_spec, dict):
            raw_name = uno_spec.get("name") if "name" in uno_spec else uno_spec.get("Name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                return self._fail("Invalid UnoCommand specification: %r" % uno_spec)
            name = raw_name
            raw_args = uno_spec.get("arguments") if "arguments" in uno_spec else uno_spec.get("Arguments")
            if raw_args is not None and not isinstance(raw_args, dict):
                return self._fail("Invalid UnoCommand specification: %r" % uno_spec)
            args = raw_args or {}
            err = self._dispatch_uno_named(name, args)
            if err is not None:
                return err
            self.applied.append("UnoCommand:%s" % name)
            return None
        elif isinstance(uno_spec, str):
            err = self._dispatch_uno_string(uno_spec)
            if err is not None:
                return err
            self.applied.append("UnoCommand")
            return None
        else:
            return self._fail("Invalid UnoCommand specification: %r" % uno_spec)

    def _execute_uno(self, name: str, props: tuple[Any, ...]) -> str | None:
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                return self._fail("UnoCommand %s failed: document has no active controller" % name)
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            dispatcher.executeDispatch(frame, name, "", 0, props)
            return None
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return self._fail("UnoCommand %s failed: %s" % (name, exc))

    def _dispatch_uno_string(self, cmd: Any, cursor: Any = None, shape: Any = None, selection_mode: str = "none") -> str | None:
        if not isinstance(cmd, str):
            return self._fail("UnoCommand requires a string: %r" % (cmd,))
        res = split_uno_command(cmd)
        if isinstance(res, str):
            return self._fail(res)
        uno_name, parsed = res
        if selection_mode == "caret":
            return self._fail("UnoCommand %s cannot be applied to a collapsed caret selection" % uno_name)
        # Range-scoped formatting must hit the SelectText cursor; selecting the
        # shape and dispatching would format every character.
        if selection_mode == "range":
            if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                return None
            return self._fail("UnoCommand %s was not applied to the text selection" % uno_name)
        if selection_mode == "all":
            if _cursor_format_pairs(uno_name, parsed) is not None:
                if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                    return None
                return self._fail("UnoCommand %s was not applied to the text selection" % uno_name)
        if selection_mode == "none":
            is_partial = _text_cursor_is_partial_selection(cursor, shape)
            if is_partial is None:
                return self._fail("UnoCommand %s failed to read text cursor" % uno_name)
            if is_partial:
                if _apply_cursor_uno_format(cursor, uno_name, parsed, warnings=self.warnings):
                    return None
                return self._fail("UnoCommand %s was not applied to the text selection" % uno_name)
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

    def _dispatch_uno_named(self, name: str, arguments: dict[str, Any]) -> str | None:
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
