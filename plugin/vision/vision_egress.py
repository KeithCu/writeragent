# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert trusted vision helper HTML results into Writer and Calc."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Literal

from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed
from plugin.framework.i18n import _
from plugin.framework.uno_context import execute_dispatch
from plugin.vision.vision_common import HELPER_NAMES, resolve_delete_image_after_insert, resolve_vision_insert_mode

log = logging.getLogger(__name__)

# Outcomes of insert_vision_result:
# - "inserted": content was inserted and no image delete was requested.
# - "image_deleted": content was inserted and the source image was removed.
# - "empty": OCR found nothing and the document was not touched.
# - "image_kept": content was inserted, but the document is recording changes
#   and LibreOffice cannot record deleting this picture.
VisionInsertOutcome = Literal["inserted", "image_deleted", "empty", "image_kept"]


def source_image_note(outcomes: Iterable[Any]) -> str:
    """Sentence about source images deleted or kept by "Delete image after OCR"; "" when neither happened."""
    items = list(outcomes)
    deleted = sum(1 for item in items if item == "image_deleted")
    kept = any(item == "image_kept" for item in items)
    parts: list[str] = []
    if deleted:
        parts.append(_("Source images deleted: {count}.").format(count=deleted))
    if kept:
        parts.append(_("Image kept because LibreOffice cannot record deleting it as a tracked change."))
    return " ".join(parts)


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
        return "VISION" in code
    return False


def vision_html_from_result(result: dict[str, Any]) -> str:
    """Return Docling/Paddle HTML payload for LO import."""
    if result.get("status") == "error":
        code = str(result.get("code") or "VISION_ERROR")
        message = str(result.get("message") or "Vision helper failed.")
        raise ToolExecutionError(message, code=code, details={"vision_result": result})

    if result.get("status") != "ok":
        raise ToolExecutionError("Vision helper returned an unexpected status.", code="VISION_ERROR", details={"vision_result": result})

    html = result.get("html")
    if html is None or not str(html).strip():
        raise ToolExecutionError("Vision helper returned empty HTML.", code="VISION_ERROR", details={"vision_result": result})
    return str(html)


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


def _writer_graphic_present(doc: Any, name: str) -> bool:
    """True when a Writer graphic still exists under *name*.

    Text flies are in ``getGraphicObjects()`` (O(1) ``hasByName``). As-char
    frames (and some imported/SdrGrafObj pictures) live only on the draw page
    and are found by ``get_graphic_object_by_name``, which is how insert
    already resolves the target. ``hasByName`` alone is a false negative for
    those and aborts a successful OCR.
    """
    if not name:
        return False
    try:
        if bool(doc.getGraphicObjects().hasByName(name)):
            return True
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("hasByName failed for %r", name, exc_info=True)
    from plugin.doc.visual_helpers import get_graphic_object_by_name

    return get_graphic_object_by_name(doc, name) is not None


def _focus_writer_frame(controller: Any) -> None:
    try:
        frame = controller.getFrame()
        if frame is None:
            return
        window = frame.getContainerWindow()
        if window is not None and hasattr(window, "setFocus"):
            window.setFocus()
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("prepare_vision_writer_insert: frame focus failed: %s", ex)


def _collapse_writer_view_cursor(controller: Any, position: Any) -> None:
    """Collapse the UI selection to *position* (text_analytics_ui insert pattern)."""
    try:
        view_cursor = controller.getViewCursor()
        view_cursor.gotoRange(position, False)
        controller.select(view_cursor)
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("prepare_vision_writer_insert: view cursor collapse failed: %s", ex)


def _dispatch_escape(controller: Any, ctx: Any) -> None:
    """Clear selection via .uno:Escape dispatch."""
    try:
        frame = controller.getFrame()
        if frame is not None and ctx is not None:
            execute_dispatch(ctx, controller, ".uno:Escape")
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("prepare_vision_writer_insert: escape dispatch failed: %s", ex)


def _same_graphic(left: Any, right: Any) -> bool:
    """True when two UNO graphic references are the same object or share a name."""
    if left is None or right is None:
        return False
    if left is right:
        return True
    try:
        left_name = str(left.getName() or "")
        right_name = str(right.getName() or "")
    except Exception:
        return False
    return bool(left_name) and left_name == right_name


def _text_content_is_graphic(node: Any, graphic: Any) -> bool:
    """True when *node* exposes this graphic as TextContent / TextEmbeddedObject."""
    for prop in ("TextContent", "TextEmbeddedObject"):
        try:
            content = node.getPropertyValue(prop)
        except Exception:
            content = getattr(node, prop, None)
        if _same_graphic(content, graphic):
            return True
    return False


def _range_holds_as_char_graphic(rng: Any, graphic: Any) -> bool:
    """True when *rng* is the as-char graphic character (Frame / TextContent).

    A text cursor does not itself have TextPortionType (UnknownPropertyException).
    After goRight(1, True), a non-empty getString() is ordinary text — the
    cursor is already past the graphic — and must not be treated as a hit
    even if content enumeration still yields an adjacent graphic. An empty
    selection is the graphic character (getString omits it); confirm via
    TextContent / portion type Frame when those are available.
    """
    try:
        if rng.getString():
            return False
    except Exception:  # noqa: S110 - UNO getString on a text cursor
        pass
    try:
        if str(rng.getPropertyValue("TextPortionType") or "") == "Frame":
            if _text_content_is_graphic(rng, graphic):
                return True
    except Exception:  # noqa: S110 - TextPortionType is absent on text cursors
        pass
    try:
        enum = rng.createContentEnumeration("com.sun.star.text.TextContent")
        while enum is not None and enum.hasMoreElements():
            if _same_graphic(enum.nextElement(), graphic):
                return True
    except Exception:  # noqa: S110 - content enumeration is optional
        pass
    # getString() omits the as-char graphic character, so a successful
    # one-character expansion that is still empty is sitting on it.
    try:
        return not rng.getString()
    except Exception:
        return False


def _should_step_over_as_char_graphic(cursor: Any, graphic: Any) -> bool:
    """True only when the cursor sits *on* an as-char graphic and must step over it.

    Writer-inserted as-char anchors may already be collapsed *after* the
    graphic. goRight(1) then skips the next text character and splits it
    (T3 → T / 3). Step only when AnchorType is AS_CHARACTER and the character
    to the right is the graphic itself (an empty one-character expansion).
    """
    from plugin.doc.visual_helpers import anchor_type_index, safe_get_property

    if anchor_type_index(safe_get_property(graphic, "AnchorType", None)) != 1:
        return False
    try:
        probe = cursor.getText().createTextCursorByRange(cursor)
        if not probe.goRight(1, True):
            return False
        return _range_holds_as_char_graphic(probe, graphic)
    except Exception:
        return False


def _enter_paragraph_after_break(cursor: Any) -> None:
    """Move into the paragraph created by the preceding insertControlCharacter.

    After a break the cursor may already sit at the start of the new
    paragraph. An unconditional goRight(1) then skips the first following
    text character (T3 → T / 3) or an as-char graphic that begins the new
    paragraph (images-only OCR can land out of order). Stay put when the
    cursor is already at the paragraph start.
    """
    try:
        text = cursor.getText()
        start = text.createTextCursorByRange(cursor)
        start.gotoStartOfParagraph(False)
        if int(text.compareRegionStarts(cursor.getStart(), start.getStart())) == 0:
            return
    except Exception:  # noqa: S110 - fall back to goRight when comparison is unavailable
        pass
    cursor.goRight(1, False)


def prepare_vision_writer_insert(doc: Any, ctx: Any, *, image_name: str | None = None) -> Any:
    """Return a collapsed text cursor in a new paragraph after the target graphic.

    StarWriter HTML import at the graphic anchor can absorb/replace the embedded
    image even when ``selected_graphic_object`` is empty. Insert a paragraph break
    at the model layer first, then collapse the UI caret there (Monaco may still
    hold focus during Run Script).

    Resolve order: *image_name*, then current single graphic selection. Collapse
    any range/graphic selection **before** anchor math and model edits so
    StarWriter does not treat a multi-character selection as the insert target
    (which would delete intervening text between images).
    """
    from plugin.doc.visual_helpers import find_image, selected_graphic_object

    name = str(image_name or "").strip()
    # Prefer name over a live selection so multi-image OCR can insert after each
    # graphic by stable name after the user's range selection was collapsed.
    resolved = find_image(doc, name)

    graphic_name = str(resolved.getName() or "")
    if name and not graphic_name:
        graphic_name = name

    controller = doc.getCurrentController()
    if controller is None:
        raise ToolExecutionError(_("Writer document has no controller."), code="VISION_ERROR")

    _focus_writer_frame(controller)
    # Clear range/graphic selection before reading anchors or inserting. A live
    # multi-character selection makes HTML import replace that range (deletes text).
    _dispatch_escape(controller, ctx)

    try:
        anchor = resolved.getAnchor()
        if anchor is None:
            raise ToolExecutionError(_("Could not resolve the image anchor for insert."), code="VISION_ERROR")
        text = anchor.getText()
        # Selection was cleared above so collapseToEnd is trustworthy (getEnd() can
        # resolve before the image while a graphic stays UI-selected).
        cursor = text.createTextCursorByRange(anchor)
        # A collapsed as-char anchor may sit *on* the graphic character
        # (collapseToEnd is then a no-op and a break lands before the image)
        # or already *after* it (goRight would skip the next text character).
        # Step over only when the character to the right is the graphic.
        # Use ``is True`` so unit-test MagicMocks still take collapseToEnd.
        if _should_step_over_as_char_graphic(cursor, resolved):
            cursor.goRight(1, False)
        else:
            try:
                collapsed = cursor.isCollapsed() is True
            except Exception:
                collapsed = False
            if not collapsed:
                cursor.collapseToEnd()
        # Model-level separator: keeps the graphic char out of the HTML import range.
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        _enter_paragraph_after_break(cursor)
    except ToolExecutionError:
        raise
    except Exception as ex:
        reraise_if_disposed(ex)
        raise ToolExecutionError(_("Could not position insert after the image: %s") % ex, code="VISION_ERROR") from ex

    _collapse_writer_view_cursor(controller, cursor.getStart())

    if selected_graphic_object(doc) is not None:
        _dispatch_escape(controller, ctx)
        _collapse_writer_view_cursor(controller, cursor.getStart())

    if graphic_name and not _writer_graphic_present(doc, graphic_name):
        raise ToolExecutionError(_("The image was removed while preparing OCR insert."), code="VISION_ERROR")
    return cursor


def ocr_found_content(result: dict[str, Any]) -> bool:
    """False when OCR ran but found no text or tables; Docling still returns skeleton HTML then."""
    if "full_text" not in result:
        return True
    return bool(str(result.get("full_text") or "").strip() or result.get("tables"))


def _delete_inserted_image(ctx: Any, doc: Any, image_name: str | None, graphic: Any | None = None) -> bool:
    """Delete *graphic* when given, else resolve *image_name* after insert, via visual_helpers.delete_graphic.

    Returns False only when LibreOffice cannot record deleting this picture as a
    tracked change (IMAGE_DELETE_NOT_TRACKABLE); all other errors are raised.
    """
    from plugin.doc.visual_helpers import delete_graphic, get_graphic_object_by_name

    target = graphic
    if target is None:
        if not image_name:
            # Without a kept shape or name, the only target would be the live selection (custom-script route).
            raise ToolExecutionError(_("Cannot delete an unnamed image after OCR insert."), code="VISION_ERROR")
        target = get_graphic_object_by_name(doc, image_name)
        if target is None:
            raise ToolExecutionError(_("The image was removed during OCR insert."), code="VISION_ERROR")
    try:
        delete_graphic(ctx, doc, target)
    except ToolExecutionError as exc:
        if exc.code == "IMAGE_DELETE_NOT_TRACKABLE":
            return False
        raise
    return True


def insert_vision_result_into_writer(
    ctx: Any, doc: Any, result: dict[str, Any], *, params: dict[str, Any] | None = None
) -> VisionInsertOutcome:
    """Insert formatted vision HTML immediately after the selected graphic anchor."""
    from plugin.writer.html_import import insert_html_at_cursor

    html = vision_html_from_result(result)
    # Log lengths and tag counts only. The document HTML is user content.
    if log.isEnabledFor(logging.DEBUG):
        log.debug("insert_vision_result: helper=%s html_len=%d h_tags=%d style_attrs=%d", result.get("helper"), len(html), html.lower().count("<h"), html.count("style="))

    image_name = str((params or {}).get("image_name") or "").strip() or None
    delete_image = resolve_delete_image_after_insert(ctx, params)

    def _apply_insert() -> VisionInsertOutcome:
        # One Ctrl+Z for insert plus optional image delete. A failed delete or
        # presence check rolls the insert back, so the image is never lost without its text.
        with UndoStepOrRollback(doc, _("WriterAgent: Insert OCR")):
            cursor = prepare_vision_writer_insert(doc, ctx, image_name=image_name)
            insert_html_at_cursor(doc, ctx, cursor, html, apply_styles=False)
            # Shipped callers always set image_name; the selection-only path was checked pre-insert.
            if image_name and not _writer_graphic_present(doc, image_name):
                raise ToolExecutionError(_("The image was removed during OCR insert."), code="VISION_ERROR")
            if delete_image:
                return "image_deleted" if _delete_inserted_image(ctx, doc, image_name) else "image_kept"
        return "inserted"

    from plugin.writer.html_import import run_writer_mutation_with_optional_review

    return run_writer_mutation_with_optional_review(doc, ctx, _apply_insert)


def insert_vision_result(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    graphic: Any | None = None,
) -> VisionInsertOutcome:
    """Insert vision output into Writer or Calc."""
    from plugin.framework.doc_type import is_calc, is_writer

    # Docling returns skeleton HTML for a blank image; inserting it only adds an empty paragraph or cell,
    # and the image-delete below must never run for an empty result.
    if not ocr_found_content(result):
        return "empty"

    if is_writer(doc):
        return insert_vision_result_into_writer(ctx, doc, result, params=params)

    from plugin.calc.vision_egress import insert_vision_html_into_calc, insert_vision_structure_into_calc

    insert_mode = resolve_vision_insert_mode(ctx, params)
    helper = str(result.get("helper") or "")
    image_name = str((params or {}).get("image_name") or "").strip() or None

    if is_calc(doc):
        delete_image = resolve_delete_image_after_insert(ctx, params)
        outcome: VisionInsertOutcome = "image_deleted" if delete_image else "inserted"
        if insert_mode == "structured" and helper == "extract_structure":
            with UndoStepOrRollback(doc, _("WriterAgent: Insert OCR")):
                try:
                    row_count = insert_vision_structure_into_calc(doc, result, image_name=image_name, graphic=graphic)
                    log.debug("insert_vision_result: helper=%s insert_mode=structured calc_rows=%d", helper, row_count)
                except ToolExecutionError as exc:
                    if exc.code != "VISION_ERROR":
                        raise
                    log.debug("structured Calc insert empty; falling back to HTML")
                    # vision_html_from_result returns result["html"] — the bbox fragment
                    # after structured mode overwrote Docling. Prefer html_docling.
                    preserved = result.get("html_docling")
                    if isinstance(preserved, str) and preserved.strip():
                        fallback_html = preserved
                    else:
                        fallback_html = vision_html_from_result(result)
                    insert_vision_html_into_calc(doc, ctx, fallback_html, image_name=image_name, graphic=graphic)
                if delete_image:
                    _delete_inserted_image(ctx, doc, image_name, graphic)
            return outcome
        html = vision_html_from_result(result)
        log.debug("insert_vision_result: helper=%s insert_mode=%s calc=html", helper, insert_mode)
        with UndoStepOrRollback(doc, _("WriterAgent: Insert OCR")):
            insert_vision_html_into_calc(doc, ctx, html, image_name=image_name, graphic=graphic)
            if delete_image:
                _delete_inserted_image(ctx, doc, image_name, graphic)
        return outcome
    raise ToolExecutionError("Vision helpers require a Writer or Calc document.", code="VISION_ERROR")


def insert_vision_item(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    params: dict[str, Any] | None,
    graphics: dict[str, Any | None],
) -> VisionInsertOutcome:
    """Insert one OCR result item by its ``image_name``, so a collapsed selection never replaces text, or by the shape kept in *graphics*."""
    insert_params = dict(params or {})
    img_name = result.get("image_name")
    if img_name:
        insert_params["image_name"] = img_name
    graphic = graphics.get(img_name) if isinstance(img_name, str) else None
    return insert_vision_result(ctx, doc, result, params=insert_params, graphic=graphic)
