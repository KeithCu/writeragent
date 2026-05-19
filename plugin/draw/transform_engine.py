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

from plugin.doc.document_helpers import WriterCompoundUndo
from plugin.draw.bridge import DrawBridge
from plugin.draw.transform_schema import get_slide_commands, is_deferred_command_key, resolve_layout_id

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)

_SET_TEXT_RE = re.compile(r"^SetText\.(\d+)$", re.I)
_EDIT_TEXT_RE = re.compile(r"^EditTextObject\.(\d+)$", re.I)
_MOVE_SLIDE_RE = re.compile(r"^MoveSlide\.(\d+)$", re.I)


def _text_shapes(page) -> list[Any]:
    shapes = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if hasattr(shape, "getString") or hasattr(shape, "getText"):
            shapes.append(shape)
    return shapes


def _set_shape_text(shape, text: str) -> None:
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


class SlideCommandEngine:
    """Execute SlideCommands array against a Draw/Impress document."""

    def __init__(self, tctx: ToolContext) -> None:
        self.tctx = tctx
        self.doc = tctx.doc
        self.bridge = DrawBridge(self.doc)
        self.pages = self.bridge.get_pages()
        self.current_slide = tctx.active_page_index if tctx.active_page_index is not None else self.bridge.get_active_page_index()
        self.applied: list[str] = []
        self.warnings: list[str] = []

    def apply(self, transform_obj: dict[str, Any]) -> dict[str, Any]:
        undo = WriterCompoundUndo(self.doc, "WriterAgent: Transform document structure")
        try:
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
        finally:
            undo.close()

    def _page_count(self) -> int:
        return self.pages.getCount()

    def _current_page(self):
        return self.pages.getByIndex(self.current_slide)

    def _apply_command(self, cmd: dict[str, Any]) -> None:
        for key, value in cmd.items():
            if is_deferred_command_key(key):
                self.warnings.append("%s is not supported in WriterAgent V1; use generate_image or atomic draw tools." % key)
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

    def _insert_master(self, master_index=None, master_name=None) -> None:
        _, new_idx = self.bridge.insert_slide_from_master(master_index=master_index, master_name=master_name, after_index=self.current_slide, switch=True)
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
        if self.current_slide >= self._page_count():
            self.current_slide = self._page_count() - 1
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
            self.current_slide = to_idx
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
                        cursor.setString(str(sv))
                    elif hasattr(shape, "setString"):
                        shape.setString(str(sv))
                elif sk == "UnoCommand":
                    self._dispatch_uno_string(sv, cursor=cursor, shape=shape)
        self.applied.append("EditTextObject.%d" % shape_index)

    def _select_text(self, xtext, cursor, spec: Any):
        if cursor is None or xtext is None:
            return cursor
        cursor.gotoStart(False)
        if spec == [] or spec is None:
            cursor.gotoEnd(True)
            return cursor
        if isinstance(spec, list) and len(spec) == 1:
            cursor.gotoStartOfParagraph(False)
            for _ in range(int(spec[0])):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.gotoEndOfParagraph(True)
            return cursor
        if isinstance(spec, list) and len(spec) >= 4:
            para, start, end_para, end_char = int(spec[0]), int(spec[1]), int(spec[2]), int(spec[3])
            cursor.gotoStart(False)
            for _ in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(start, False)
            for _ in range(end_para - para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(end_char, True)
            return cursor
        if isinstance(spec, list) and len(spec) == 2:
            para, char = int(spec[0]), int(spec[1])
            cursor.gotoStart(False)
            for _ in range(para):
                if not cursor.gotoNextParagraph(False):
                    break
            cursor.goRight(char, False)
            return cursor
        return cursor

    def _select_paragraph(self, xtext, cursor, para_index: int):
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

    def _dispatch_uno_string(self, cmd: Any, cursor=None, shape=None) -> None:
        if not isinstance(cmd, str):
            return
        cmd = cmd.strip()
        if not cmd:
            return
        # ".uno:Bold" or '.uno:Color {"Color.Color":...}'
        parts = cmd.split(None, 1)
        uno_name = parts[0]
        arg_json = parts[1] if len(parts) > 1 else None
        props = ()
        if arg_json:
            from plugin.framework.json_utils import safe_json_loads

            parsed = safe_json_loads(arg_json, default={})
            if isinstance(parsed, dict):
                props = self._uno_props_from_dict(parsed)
        try:
            controller = self.doc.getCurrentController()
            if controller is None:
                return
            frame = controller.getFrame()
            smgr = self.tctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", self.tctx.ctx)
            if cursor is not None and shape is not None:
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
