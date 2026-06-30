# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import ppt-master SVG slides via LibreOffice draw_svg_import filter."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.contrib.ppt_master.svg_preprocess import preprocess_svg_for_import
from plugin.draw.bridge import DrawBridge
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_shape_postprocess import (
    _page_size_hmm,
    clear_page_shapes,
    copy_shapes_to_page,
    postprocess_slide_shapes,
)

log = logging.getLogger(__name__)

_SVG_IMPORT_FILTER = "draw_svg_import"


def _load_props(hidden: bool = True) -> tuple[Any, ...]:
    import uno

    return (
        uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=hidden),
        uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="AsTemplate", Value=True),
        uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="FilterName", Value=_SVG_IMPORT_FILTER),
    )


def _load_svg_as_draw_doc(desktop: Any, svg_uri: str) -> Any | None:
    try:
        doc = desktop.loadComponentFromURL(svg_uri, "_blank", 0, _load_props())
        if doc is None:
            return None
        pages = doc.getDrawPages()
        if pages.getCount() < 1 or pages.getByIndex(0).getCount() < 1:
            log.warning("draw_svg_import produced empty page for %s", svg_uri)
            return doc
        return doc
    except Exception as exc:
        log.warning("draw_svg_import failed for %s: %s", svg_uri, exc)
        return None


def _apply_notes(doc: Any, page: Any, notes_text: str | None) -> None:
    if not notes_text or not doc.supportsService("com.sun.star.presentation.PresentationDocument"):
        return
    try:
        notes_page = page.getNotesPage()
        for i in range(notes_page.getCount()):
            shape = notes_page.getByIndex(i)
            if hasattr(shape, "setString"):
                shape.setString(notes_text)
                break
    except Exception as exc:
        log.debug("notes: %s", exc)


def _ensure_target_page(bridge: DrawBridge, slide_index: int, *, clear: bool = True) -> Any:
    pages = bridge.get_pages()
    while pages.getCount() <= slide_index:
        bridge.create_slide(pages.getCount(), switch=False)
    page = pages.getByIndex(slide_index)
    if clear:
        clear_page_shapes(page)
    bridge.set_current_page_index(slide_index)
    return page


def import_svg_to_slide(
    ctx: Any,
    target_doc: Any,
    svg_path: Path,
    *,
    slide_index: int = 0,
    project_dir: Path | None = None,
    notes_text: str | None = None,
    clear_slide: bool = True,
) -> dict[str, Any]:
    """Pre-process SVG, import via LO filter, copy shapes onto Impress slide."""
    svg_path = Path(svg_path).expanduser().resolve()
    preprocessed: Path | None = None
    temp_doc = None
    try:
        preprocessed = preprocess_svg_for_import(svg_path, project_dir=project_dir)
        desktop = get_desktop(ctx)
        temp_doc = _load_svg_as_draw_doc(desktop, preprocessed.as_uri())
        if temp_doc is None:
            return {"status": "error", "message": f"SVG import failed: {svg_path.name}"}

        source_page = temp_doc.getDrawPages().getByIndex(0)
        src_w, src_h = _page_size_hmm(source_page)
        bridge = DrawBridge(target_doc)
        target_page = _ensure_target_page(bridge, slide_index, clear=clear_slide)
        copied = copy_shapes_to_page(source_page, target_doc, target_page)
        if copied < 1:
            return {"status": "error", "message": f"No shapes copied from {svg_path.name}"}

        post = postprocess_slide_shapes(
            target_page,
            source_page_width_hmm=src_w,
            source_page_height_hmm=src_h,
            target_width_hmm=DEFAULT_SLIDE_WIDTH_HMM,
            target_height_hmm=DEFAULT_SLIDE_HEIGHT_HMM,
        )
        _apply_notes(target_doc, target_page, notes_text)
        return {
            "status": "ok",
            "slide_index": slide_index,
            "shapes_copied": copied,
            "route": "draw_svg_import",
            **post,
        }
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception as exc:
                log.debug("close temp svg doc: %s", exc)
        if preprocessed is not None and preprocessed != svg_path:
            try:
                os.unlink(preprocessed)
            except OSError as exc:
                log.debug("unlink preprocessed svg: %s", exc)


def import_svg_files_to_doc(
    ctx: Any,
    doc: Any,
    svg_files: list[Path],
    *,
    project_dir: Path | None = None,
    notes_by_index: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Import one SVG per slide index."""
    results: list[dict[str, Any]] = []
    notes = notes_by_index or {}
    for i, svg_path in enumerate(svg_files):
        result = import_svg_to_slide(
            ctx,
            doc,
            svg_path,
            slide_index=i,
            project_dir=project_dir,
            notes_text=notes.get(i),
        )
        results.append(result)
        if result.get("status") != "ok":
            return {"status": "error", "message": result.get("message", "import failed"), "results": results}
    return {"status": "ok", "slides": len(results), "route": "draw_svg_import", "results": results}
