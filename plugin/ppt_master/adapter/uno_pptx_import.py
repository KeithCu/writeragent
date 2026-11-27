# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import ppt-master PPTX into Impress via LibreOffice's native PPTX filter.

Picture shapes are reimported in ``shape_clone`` so closing the
hidden source does not dispose a graphic the target still references. A
slide is emptied of its previous shapes only after that slide's copy
reports at least one shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.draw.bridge import DrawBridge, notes_text, set_notes_text
from plugin.draw.shape_clone import clone_shape
from plugin.framework.errors import ToolExecutionError, best_effort, format_error_payload, is_disposal
from plugin.framework.scratch_writer import close_scratch_document, load_scratch_document
from plugin.framework.uno_context import create_property_value, get_desktop
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger(__name__)


def copy_shapes_to_page(source_page: Any, target_doc: Any, target_page: Any, uno_ctx: Any | None = None) -> int:
    """Copy all shapes from *source_page* to *target_page*; return count copied.

    A source shape that cannot be cloned is skipped and leaves nothing on *target_page*.
    """
    copied = 0
    for i in range(source_page.getCount()):
        src = source_page.getByIndex(i)
        try:
            result = clone_shape(target_doc, target_page, src, uno_ctx=uno_ctx)
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.warning("clone_shape failed: %s", exc)
            continue
        if result.empty_group_message is not None:
            try:
                target_page.remove(result.shape)
            except Exception as exc:
                if is_disposal(exc):
                    raise
                # Non-disposal remove failure leaves an unplaced clone on the page;
                # log so it is recorded why removal failed.
                log.debug("remove empty-group clone failed", exc_info=True)
            log.warning("clone_shape empty group: %s", result.empty_group_message)
            continue
        if result.graphic_shapes > result.graphic_reimports:
            log.warning("PPTX picture reimport failed; left Graphic unset rather than alias the hidden source pool")
        copied += 1
    return copied


def load_pptx_as_impress_doc(ctx: Any, pptx_path: Path) -> Any | None:
    """Open a PPTX as a hidden Impress document (LO extension-based filter)."""
    if not pptx_path.is_file():
        return None
    try:
        # ReadOnly: an editable load shares the lock file of a deck the user has open, and
        # closing the copy then deletes that file (SfxMedium::LockOrigFileOnDemand,
        # SfxMedium::UnlockFile in sfx2/source/doc/docfile.cxx).
        doc = load_scratch_document(
            get_desktop(ctx), pptx_path.as_uri(), props=(create_property_value("ReadOnly", True),)
        )
        if doc is not None:
            # Drop the URL so File > Open and document_open load the file themselves instead of
            # this hidden copy (LoadEnv::impl_searchAlreadyLoaded, resolve_document_by_url).
            doc.attachResource("", ())
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
    page.setPropertyValue("Width", DEFAULT_SLIDE_WIDTH_HMM)
    page.setPropertyValue("Height", DEFAULT_SLIDE_HEIGHT_HMM)


def _page_shapes(page: Any) -> list[Any]:
    return [page.getByIndex(i) for i in range(page.getCount())]


def _drop_page(pages: Any, page: Any) -> None:
    best_effort(lambda: pages.remove(page), "drop unused import page")


def _copy_page_notes(source_page: Any, target_page: Any) -> None:
    """Copy the source NotesShape onto the target NotesShape, including "".

    Slide shape cleanup never touches the notes page, so write empty notes
    too; otherwise a re-import of empty notes leaves the previous slide's text.
    """
    try:
        raw = notes_text(source_page)
        notes_str = "" if raw is None else raw.strip()
        set_notes_text(target_page, notes_str)
    except Exception as exc:
        if is_disposal(exc):
            raise
        # A non-disposed notes error (corrupt NotesShape) is logged and this
        # slide's notes are skipped so the rest of the deck still imports.
        # Re-raising aborts the import after earlier slides were replaced.
        # A disposed document still re-raises.
        log.warning("Skipping slide notes copy due to error: %s", exc)


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
        return format_error_payload(ToolExecutionError("PPTX contains no slides."))

    indices = slide_indices if slide_indices is not None else list(range(source_count))
    # Validate every index before replacing slides so a bad index does not commit partial imports.
    for src_index in indices:
        if src_index < 0 or src_index >= source_count:
            return format_error_payload(ToolExecutionError(f"PPTX slide index out of range: {src_index}"))

    bridge = DrawBridge(target_doc)
    pages = bridge.get_pages()
    results: list[dict[str, Any]] = []
    for out_index, src_index in enumerate(indices):
        source_page = source_pages.getByIndex(src_index)
        # Copy first, and remove the previous shapes only after this slide's
        # copy reports at least one shape, because clearing first would lose
        # the slide when nothing copies. ``copy_shapes_to_page`` leaves no shape
        # behind for a source shape that failed, so a copy with no successes
        # only has to drop a slide this call created. Previous shapes are
        # recorded and removed as objects because ``clone_shape`` copies ZOrder,
        # which moves each clone to its source index
        # (svx/source/unodraw/unoshape.cxx OWN_ATTR_ZORDER), so positions do
        # not tell old shapes from new ones.
        replace = clear_existing or out_index > 0
        existed = out_index < int(pages.getCount())
        target_page = _ensure_target_page(bridge, out_index)
        previous = _page_shapes(target_page)
        copied = copy_shapes_to_page(source_page, target_doc, target_page, uno_ctx=ctx)
        if copied < 1:
            if not existed:
                _drop_page(pages, target_page)
            return format_error_payload(
                ToolExecutionError(f"No shapes copied from PPTX slide {src_index + 1}")
            )
        if replace:
            for shape in previous:
                target_page.remove(shape)
        _apply_target_page_size(target_page)
        bridge.set_current_page_index(out_index)
        _copy_page_notes(source_page, target_page)
        results.append({"slide_index": out_index, "source_slide_index": src_index, "shapes_copied": copied})

    return {"status": "ok", "slides": len(results), "route": "pptx_to_odp", "results": results}


# UNO load/save and slide copy; callers off the main thread must not enter here
# (export_project_to_doc and evaluate_slide_fidelity marshal via main.call).
@main_thread_only
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
        return format_error_payload(ToolExecutionError(f"PPTX import failed: {pptx_path.name}"))
    try:
        if save_mirror_odp is not None:
            save_mirror_odp.parent.mkdir(parents=True, exist_ok=True)
            source_doc.storeToURL(save_mirror_odp.as_uri(), ())
        result = _import_slides_from_source(ctx, target_doc, source_doc, clear_existing=clear_existing)
        if result.get("status") == "ok":
            result["pptx_path"] = str(pptx_path)
            if save_mirror_odp is not None:
                result["mirror_odp"] = str(save_mirror_odp)
        return result
    finally:
        best_effort(lambda: close_scratch_document(source_doc), "close source pptx doc")


@main_thread_only
def import_pptx_slide_to_odp(
    ctx: Any,
    pptx_path: Path,
    slide_index: int,
    odp_path: Path,
) -> tuple[Any, Any] | None:
    """Import one PPTX slide into a new one-slide Impress doc and save ODP.

    The caller closes the returned document with ``close_scratch_document``.
    """
    pptx_path = Path(pptx_path).expanduser().resolve()
    source_doc = load_pptx_as_impress_doc(ctx, pptx_path)
    if source_doc is None:
        return None
    target_doc = None
    imported: tuple[Any, Any] | None = None
    try:
        target_doc = load_scratch_document(get_desktop(ctx), "private:factory/simpress")
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
            return None
        page = target_doc.getDrawPages().getByIndex(0)
        odp_path.parent.mkdir(parents=True, exist_ok=True)
        target_doc.storeToURL(odp_path.as_uri(), ())
        imported = (target_doc, page)
        return imported
    finally:
        if target_doc is not None and imported is None:
            best_effort(lambda: close_scratch_document(target_doc), "close unused import target")
        best_effort(lambda: close_scratch_document(source_doc), "close source pptx doc")
