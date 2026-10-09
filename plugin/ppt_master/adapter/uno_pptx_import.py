# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import ppt-master PPTX into Impress via LibreOffice's native PPTX filter.

Picture shapes are reimported in ``uno_shape_postprocess`` so closing the
hidden source does not dispose a graphic the target still references. A
slide is emptied of its previous shapes only after that slide's copy
reports at least one shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.draw.bridge import DrawBridge, find_notes_shape
from plugin.framework.errors import is_disposed_exception
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_shape_postprocess import copy_shapes_to_page
from plugin.framework.queue_executor import execute_on_main_thread

log = logging.getLogger(__name__)


def _hidden_load_props() -> tuple[Any, ...]:
    import uno

    return (uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True),)


def load_pptx_as_impress_doc(ctx: Any, pptx_path: Path) -> Any | None:
    """Open a PPTX as a hidden Impress document (LO extension-based filter)."""
    pptx_path = Path(pptx_path).expanduser().resolve()
    if not pptx_path.is_file():
        return None
    try:
        desktop = get_desktop(ctx)
        doc = desktop.loadComponentFromURL(pptx_path.resolve().as_uri(), "_blank", 0, _hidden_load_props())
        return doc
    except Exception as exc:
        log.warning("PPTX load failed for %s: %s", pptx_path, exc)
        return None


def _ensure_target_page(bridge: DrawBridge, slide_index: int) -> Any:
    """Return the draw page at *slide_index*, creating blank slides if needed.

    Does not remove shapes or resize. A failed copy must leave an existing
    slide's content and size alone; see ``_import_slides_from_source``.
    """
    pages = bridge.get_pages()
    while pages.getCount() <= slide_index:
        bridge.create_slide(pages.getCount(), switch=False)
    return pages.getByIndex(slide_index)


def _apply_target_page_size(page: Any) -> None:
    try:
        page.setPropertyValue("Width", DEFAULT_SLIDE_WIDTH_HMM)
        page.setPropertyValue("Height", DEFAULT_SLIDE_HEIGHT_HMM)
    except Exception as exc:
        log.debug("set target page size: %s", exc)


def _trim_appended_shapes(page: Any, keep_count: int) -> None:
    """Drop shapes ``add`` appended past *keep_count*.

    ``clone_shape_to_page`` appends. A copy that reports no successes can
    still have added a shape before that clone failed; those are not user
    content and are removed from the end.
    """
    while True:
        try:
            count = int(page.getCount())
        except Exception as exc:
            log.debug("trim appended shapes: %s", exc)
            return
        if count <= keep_count:
            return
        try:
            page.remove(page.getByIndex(count - 1))
        except Exception as exc:
            log.debug("trim appended shapes: %s", exc)
            return


def _drop_front_shapes(page: Any, drop_count: int) -> None:
    """Remove the shapes that were on the page before a successful append."""
    remaining = drop_count
    while remaining > 0:
        remaining -= 1
        try:
            if int(page.getCount()) < 1:
                return
            page.remove(page.getByIndex(0))
        except Exception as exc:
            log.debug("drop previous shapes: %s", exc)
            return


def _drop_page(pages: Any, page: Any) -> None:
    try:
        pages.remove(page)
    except Exception as exc:
        log.debug("drop unused import page: %s", exc)


def _copy_page_notes(source_page: Any, target_page: Any) -> None:
    """Copy the source NotesShape onto the target NotesShape, including "".

    find_notes_shape is the NotesShape lookup the notes tools use. Header,
    footer, and date fields also implement getString/setString and can
    precede the NotesShape. clear_page_shapes only removes slide shapes, so
    write the notes shape even when the text is empty; otherwise a re-import
    of empty notes leaves the previous text (or writes chrome into the first
    setString shape).
    """
    try:
        src_shape = find_notes_shape(source_page.getNotesPage())
        # No NotesShape means the source has no speaker-notes body. hasattr
        # is the wrong probe: a PyUNO method can be callable and still fail
        # hasattr, and that false miss would look like empty notes.
        notes_text = ""
        if src_shape is not None:
            notes_text = str(src_shape.getString() or "").strip()
        tgt_shape = find_notes_shape(target_page.getNotesPage())
        if tgt_shape is None:
            return
        tgt_shape.setString(notes_text)
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        # A non-disposed notes error (corrupt NotesShape) is logged and this
        # slide's notes are skipped so the rest of the deck still imports.
        # Re-raising aborts the import after earlier slides were replaced.
        # A disposed document still re-raises.
        log.warning("Skipping slide notes copy due to error: %s", exc)
        return


def _import_slides_from_source(
    ctx: Any,
    target_doc: Any,
    source_doc: Any,
    *,
    slide_indices: list[int] | None = None,
    clear_existing: bool = True,
) -> dict[str, Any]:
    source_pages = source_doc.getDrawPages()
    source_count = int(source_pages.getCount())
    if source_count < 1:
        return {"status": "error", "message": "PPTX contains no slides."}

    indices = slide_indices if slide_indices is not None else list(range(source_count))
    # Reject a bad index before any slide is replaced. Doing it inside the
    # loop used to commit earlier slides and then fail.
    for src_index in indices:
        if src_index < 0 or src_index >= source_count:
            return {"status": "error", "message": f"PPTX slide index out of range: {src_index}"}

    bridge = DrawBridge(target_doc)
    pages = bridge.get_pages()
    results: list[dict[str, Any]] = []
    for out_index, src_index in enumerate(indices):
        source_page = source_pages.getByIndex(src_index)
        # Append shapes first. Remove the previous shapes only after this
        # slide's copy reports at least one shape. Clearing first
        # (_ensure_target_page(..., clear=True) then copy_shapes_to_page)
        # destroys user content when the copy returns nothing. A failed copy
        # trims what it appended, or drops a slide this call just created,
        # and leaves an existing slide's shapes in place.
        replace = clear_existing or out_index > 0
        existed = execute_on_main_thread(lambda: out_index < int(pages.getCount()))
        target_page = execute_on_main_thread(lambda: _ensure_target_page(bridge, out_index))
        try:
            previous_count = execute_on_main_thread(lambda: int(target_page.getCount()))
        except Exception as exc:
            log.debug("count target shapes: %s", exc)
            previous_count = None
        copied = execute_on_main_thread(lambda: copy_shapes_to_page(source_page, target_doc, target_page, uno_ctx=ctx))
        if copied < 1:
            if not existed:
                execute_on_main_thread(lambda: _drop_page(pages, target_page))
            elif previous_count is not None:
                trim_count = previous_count
                execute_on_main_thread(lambda: _trim_appended_shapes(target_page, trim_count))
            return {"status": "error", "message": f"No shapes copied from PPTX slide {src_index + 1}"}
        if replace and previous_count is not None:
            drop_count = previous_count
            execute_on_main_thread(lambda: _drop_front_shapes(target_page, drop_count))
        execute_on_main_thread(lambda: _apply_target_page_size(target_page))
        execute_on_main_thread(lambda: bridge.set_current_page_index(out_index))
        execute_on_main_thread(lambda: _copy_page_notes(source_page, target_page))
        results.append({"slide_index": out_index, "source_slide_index": src_index, "shapes_copied": copied})

    return {"status": "ok", "slides": len(results), "route": "pptx_to_odp", "results": results}


def import_pptx_to_doc(
    ctx: Any,
    target_doc: Any,
    pptx_path: Path,
    *,
    clear_existing: bool = True,
    save_mirror_odp: Path | None = None,
) -> dict[str, Any]:
    """Load PPTX hidden, copy all slides into *target_doc*, optionally write mirror ODP."""
    pptx_path = Path(pptx_path).expanduser().resolve()
    source_doc = load_pptx_as_impress_doc(ctx, pptx_path)
    if source_doc is None:
        return {"status": "error", "message": f"PPTX import failed: {pptx_path.name}"}
    try:
        if save_mirror_odp is not None:
            save_mirror_odp = Path(save_mirror_odp).expanduser().resolve()
            save_mirror_odp.parent.mkdir(parents=True, exist_ok=True)
            source_doc.storeToURL(save_mirror_odp.as_uri(), ())
        result = _import_slides_from_source(ctx, target_doc, source_doc, clear_existing=clear_existing)
        if result.get("status") == "ok":
            result["pptx_path"] = str(pptx_path)
            if save_mirror_odp is not None:
                result["mirror_odp"] = str(save_mirror_odp)
        return result
    finally:
        try:
            source_doc.close(True)
        except Exception as exc:
            log.debug("close source pptx doc: %s", exc)


def import_pptx_slide_to_odp(
    ctx: Any,
    pptx_path: Path,
    slide_index: int,
    odp_path: Path,
) -> tuple[Any, Any] | None:
    """Import one PPTX slide into a new one-slide Impress doc and save ODP."""
    import uno

    pptx_path = Path(pptx_path).expanduser().resolve()
    source_doc = load_pptx_as_impress_doc(ctx, pptx_path)
    if source_doc is None:
        return None
    target_doc = None
    try:
        desktop = get_desktop(ctx)
        hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
        target_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden,))
        if target_doc is None:
            return None
        result = _import_slides_from_source(
            ctx,
            target_doc,
            source_doc,
            slide_indices=[slide_index],
            clear_existing=True,
        )
        if result.get("status") != "ok":
            target_doc.close(True)
            return None
        page = target_doc.getDrawPages().getByIndex(0)
        odp_path = Path(odp_path).expanduser().resolve()
        odp_path.parent.mkdir(parents=True, exist_ok=True)
        target_doc.storeToURL(odp_path.as_uri(), ())
        return target_doc, page
    finally:
        try:
            source_doc.close(True)
        except Exception as exc:
            log.debug("close source pptx doc: %s", exc)
