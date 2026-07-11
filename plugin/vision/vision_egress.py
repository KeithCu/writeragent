# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert trusted vision helper HTML results into Writer and Calc."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.vision.vision_common import HELPER_NAMES, resolve_vision_insert_mode

log = logging.getLogger(__name__)


def is_vision_result(value: Any) -> bool:
    """True when *value* matches the compact vision helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        return code == "VISION_ERROR" or "VISION" in code
    return False


def vision_html_from_result(result: dict[str, Any]) -> str:
    """Return Docling/Paddle HTML payload for LO import."""
    if result.get("status") == "error":
        code = str(result.get("code") or "VISION_ERROR")
        message = str(result.get("message") or "Vision helper failed.")
        raise ToolExecutionError(message, code=code, details={"vision_result": result})

    if result.get("status") != "ok":
        raise ToolExecutionError(
            "Vision helper returned an unexpected status.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )

    html = result.get("html")
    if html is None:
        raise ToolExecutionError(
            "Vision helper result is missing html.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )
    return str(html)


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


def _focus_writer_frame(controller: Any) -> None:
    try:
        frame = controller.getFrame()
        if frame is None:
            return
        window = frame.getContainerWindow()
        if window is not None and hasattr(window, "setFocus"):
            window.setFocus()
    except Exception as ex:
        log.debug("prepare_vision_writer_insert: frame focus failed: %s", ex)


def _collapse_writer_view_cursor(controller: Any, position: Any) -> None:
    """Collapse the UI selection to *position* (text_analytics_ui insert pattern)."""
    try:
        view_cursor = controller.getViewCursor()
        view_cursor.gotoRange(position, False)
        controller.select(view_cursor)
    except Exception as ex:
        log.debug("prepare_vision_writer_insert: view cursor collapse failed: %s", ex)


def prepare_vision_writer_insert(doc: Any, ctx: Any, *, image_name: str | None = None) -> Any:
    """Return a collapsed text cursor in a new paragraph after the selected graphic.

    StarWriter HTML import at the graphic anchor can absorb/replace the embedded
    image even when ``selected_graphic_object`` is empty. Insert a paragraph break
    at the model layer first, then collapse the UI caret there (Monaco may still
    hold focus during Run Script).
    """
    from plugin.doc.visual_helpers import get_graphic_object_by_name, list_graphic_objects, selected_graphic_object

    graphic = selected_graphic_object(doc)
    name = str(image_name or "").strip()
    if graphic is None and name:
        graphic = get_graphic_object_by_name(doc, name)
    if graphic is None:
        raise ToolExecutionError(
            _("Select an embedded image, then Run again."),
            code="NO_IMAGE_SELECTED",
        )

    graphic_name = ""
    try:
        graphic_name = str(graphic.getName() or "")
    except Exception:
        pass
    graphics_before = len(list_graphic_objects(doc))

    try:
        anchor = graphic.getAnchor()
        if anchor is None:
            raise ToolExecutionError(
                _("Could not resolve the image anchor for insert."),
                code="VISION_ERROR",
            )
        text = anchor.getText()
        # Step past the in-text graphic character (getEnd() can resolve before the image
        # while the graphic stays UI-selected).
        cursor = text.createTextCursorByRange(anchor.getStart())
        if not cursor.goRight(1, False):
            cursor = text.createTextCursorByRange(anchor)
            cursor.collapseToEnd()
        # Model-level separator: keeps the graphic char out of the HTML import range.
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        cursor.goRight(1, False)
    except ToolExecutionError:
        raise
    except Exception as ex:
        raise ToolExecutionError(
            _("Could not position insert after the image: %s") % ex,
            code="VISION_ERROR",
        ) from ex

    controller = doc.getCurrentController()
    if controller is None:
        raise ToolExecutionError(
            _("Writer document has no controller."),
            code="VISION_ERROR",
        )

    _focus_writer_frame(controller)
    _collapse_writer_view_cursor(controller, cursor.getStart())

    if selected_graphic_object(doc) is not None:
        try:
            frame = controller.getFrame()
            if frame is not None and ctx is not None:
                smgr = ctx.ServiceManager
                dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
                dispatcher.executeDispatch(frame, ".uno:Escape", "", 0, ())
                _collapse_writer_view_cursor(controller, cursor.getStart())
        except Exception as ex:
            log.debug("prepare_vision_writer_insert: escape fallback failed: %s", ex)

    if graphic_name and get_graphic_object_by_name(doc, graphic_name) is None:
        raise ToolExecutionError(
            _("The image was removed while preparing OCR insert."),
            code="VISION_ERROR",
        )
    if len(list_graphic_objects(doc)) < graphics_before:
        raise ToolExecutionError(
            _("The image was removed while preparing OCR insert."),
            code="VISION_ERROR",
        )
    return cursor


def insert_vision_result_into_writer(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
) -> None:
    """Insert formatted vision HTML immediately after the selected graphic anchor."""
    from plugin.writer.format import insert_html_at_cursor

    html = vision_html_from_result(result)
    if not html.strip():
        raise ToolExecutionError(
            "Vision helper returned empty HTML.",
            code="VISION_ERROR",
            details={"vision_result": result},
        )
    log.debug(
        "insert_vision_result: helper=%s html_len=%d h_tags=%d style_attrs=%d snippet=%r",
        result.get("helper"),
        len(html),
        html.lower().count("<h"),
        html.count("style="),
        html[:120],
    )

    params_dict = dict(params) if isinstance(params, dict) else {}
    image_name = str(params_dict.get("image_name") or "").strip() or None

    def _apply_insert() -> None:
        from plugin.doc.visual_helpers import list_graphic_objects

        graphics_before = len(list_graphic_objects(doc))
        cursor = prepare_vision_writer_insert(doc, ctx, image_name=image_name)
        insert_html_at_cursor(doc, ctx, cursor, html, apply_styles=False)
        if len(list_graphic_objects(doc)) < graphics_before:
            raise ToolExecutionError(
                _("The image was removed during OCR insert."),
                code="VISION_ERROR",
            )

    from plugin.writer.format import run_writer_mutation_with_optional_review

    run_writer_mutation_with_optional_review(doc, ctx, _apply_insert)


def insert_vision_result(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
) -> None:
    """Insert vision output into Writer or Calc."""
    from plugin.calc.vision_egress import insert_vision_html_into_calc, insert_vision_structure_into_calc
    from plugin.doc.document_helpers import is_calc, is_writer

    insert_mode = resolve_vision_insert_mode(ctx, params)
    helper = str(result.get("helper") or "")

    if is_writer(doc):
        insert_vision_result_into_writer(ctx, doc, result, params=params)
        return
    if is_calc(doc):
        if insert_mode == "structured" and helper == "extract_structure":
            try:
                row_count = insert_vision_structure_into_calc(doc, ctx, result)
                log.debug(
                    "insert_vision_result: helper=%s insert_mode=structured calc_rows=%d",
                    helper,
                    row_count,
                )
                return
            except ToolExecutionError as exc:
                if exc.code != "VISION_ERROR":
                    raise
                log.debug("structured Calc insert empty; falling back to HTML")
        html = vision_html_from_result(result)
        if not html.strip():
            raise ToolExecutionError(
                "Vision helper returned empty HTML.",
                code="VISION_ERROR",
                details={"vision_result": result},
            )
        log.debug("insert_vision_result: helper=%s insert_mode=%s calc=html", helper, insert_mode)
        insert_vision_html_into_calc(doc, ctx, html)
        return
    raise ToolExecutionError(
        "Vision helpers require a Writer or Calc document.",
        code="VISION_ERROR",
    )
