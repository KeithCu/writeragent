# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer/Draw page-to-PNG rendering helpers (shared by image_get and vision preview)."""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, cast

from plugin.doc.text_helpers import clone_text_range, jump_view_cursor_to_page
from plugin.framework.errors import best_effort, reraise_if_disposed
from plugin.framework.thread_guard import unwrap_uno

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def write_page_png(page: int, export: Callable[[str], None]) -> tuple[bytes | None, str | None]:
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="wa_page_render_", suffix=".png")
        os.close(fd)
        export(tmp_path)
        with open(tmp_path, "rb") as f:
            png = f.read()
        if png[:8] != PNG_MAGIC:
            return None, "could not render page %d: the PNG export produced no valid image." % page
        return png, None
    except Exception as e:
        reraise_if_disposed(e)
        return None, "could not render page %d: %s" % (page, e)
    finally:
        if tmp_path:
            best_effort(lambda: os.remove(tmp_path), "page render: remove temp png failed")


def render_writer_page_png(
    doc: Any,
    page: int,
    *,
    pixel_size: tuple[int, int] | None = None,
) -> tuple[bytes | None, str | None]:
    """Render 0-based *page* of a Writer doc to PNG bytes, or (None, reason).

    *page* is model-facing (first page = 0); jump_view_cursor_to_page converts it to Writer's
    1-based page — error text still quotes the 0-based index the model sent.

    NATIVE LibreOffice path ONLY — the writer_png_Export graphic filter, targeted per page with the
    view cursor (jumpToPage): cross-platform, no external binary, no PDF round-trip, and the filter
    reuses LO's own page painting (headers, frames, shapes, images all faithful). XRenderable
    is not used: on real multi-page documents its getRendererCount reports 1 page whatever
    options are passed, and its getDIB() bitmaps are build-dependent. The view cursor is saved and
    restored best-effort (the jump may briefly scroll the window). There is intentionally NO fallback — on
    any failure this returns a clear reason (surfaced by the caller as a tool error), never a
    silent failure or an empty image.
    """
    try:
        vc = doc.getCurrentController().getViewCursor()
    except Exception as e:
        # Disposal must reach execute_safe as DOCUMENT_DISPOSED, not a render reason.
        reraise_if_disposed(e)
        return None, "could not render page %d: no document view available (%s)" % (page, e)

    # Save the view cursor so the page jump can be undone. clone_text_range
    # copies it through the cursor's own XText; if that raises, the restore
    # below is skipped.
    saved = best_effort(lambda: clone_text_range(vc), "save view cursor before page render")

    try:
        total = jump_view_cursor_to_page(vc, page)
        if total is not None:
            return None, "could not render page %d: page not found (document has %d page(s))." % (page, total)
        # UNO imports only here: nothing above needs them, so validation/error paths stay
        # exception-free even where the uno module is unavailable (e.g. mocked test envs).
        import uno
        from com.sun.star.beans import PropertyValue

        def export(tmp_path: str) -> None:
            url = uno.systemPathToFileUrl(tmp_path)
            if pixel_size is None:
                doc.storeToURL(url, (PropertyValue(Name="FilterName", Value="writer_png_Export"),))
                return
            w, h = pixel_size
            # FilterData must travel as a typed Any through uno.invoke; a plain tuple is dropped and the
            # page renders at 96 DPI (same as image_mark.py:217-221). GraphicExportFilter ignores the
            # target unless both PixelWidth and PixelHeight are non-zero (GraphicExportFilter.cxx filterRenderDocument).
            filter_data = getattr(uno, "Any")(
                "[]com.sun.star.beans.PropertyValue",
                (PropertyValue(Name="PixelWidth", Value=w), PropertyValue(Name="PixelHeight", Value=h)),
            )
            uno.invoke(
                unwrap_uno(doc),
                "storeToURL",
                cast(
                    "tuple[Any, ...]",
                    (
                        url,
                        (
                            PropertyValue(Name="FilterName", Value="writer_png_Export"),
                            PropertyValue(Name="FilterData", Value=filter_data),
                        ),
                    ),
                ),
            )

        return write_page_png(page, export)
    except Exception as e:
        reraise_if_disposed(e)
        return None, "could not render page %d: %s" % (page, e)
    finally:
        if saved is not None:
            best_effort(lambda: vc.gotoRange(saved, False), "restore view cursor after page render")
