# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden scratch Writer creation and cleanup helpers.

Split out from ``uno_context`` to separate scratch document lifecycle
from core context and desktop lookup.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import DocumentDisposedError, is_real_disposal
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger("writeragent.scratch_writer")


def _guard_returned_uno(obj: Any) -> Any:
    """Wrap a UNO boundary return. Imports ``guard_uno`` at call time for test patching."""
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(obj)


def _close_scratch_doc(doc: Any) -> None:
    """Close a hidden scratch document we are about to abandon, so it does not leak."""
    try:
        doc.close(True)
    except Exception:
        log.debug("new_blank_writer: doc.close() failed", exc_info=True)


def _reraise_document_disposed(exc: BaseException, object_type: str) -> None:
    """Re-raise real UNO disposal. Other exceptions stay with the caller.

    Only real disposal (not a bare RuntimeException) becomes
    DocumentDisposedError. Catching Exception and treating a disposed
    document as empty or not open swallows DisposedException.
    """
    if not is_real_disposal(exc):
        return
    if isinstance(exc, DocumentDisposedError):
        raise exc
    raise DocumentDisposedError(str(exc) or "UNO object was disposed", object_type=object_type) from exc


@main_thread_only
def clear_writer_body(doc: Any) -> bool:
    """Empty *doc* of everything a template can put in it. True when something was removed.

    Not just the body text: a letterhead template is often an empty table or a logo
    anchored to the page, whose body string is "" -- testing the text alone left that
    table in the scratch doc, and it came back in range reads. So tables, text frames
    and drawing shapes are disposed explicitly, then the text is cleared.

    Split out so callers that open (or reuse) a scratch Writer their own way can
    still drop a default template's content.
    """
    if doc is None:
        return False
    removed = False
    for supplier in ("getTextTables", "getTextFrames"):
        try:
            container = getattr(doc, supplier)()
            names = list(container.getElementNames())
        except Exception as e:
            _reraise_document_disposed(e, "Writer")
            continue
        for name in names:
            try:
                if container.hasByName(name):  # a nested table goes with its parent
                    container.getByName(name).dispose()
                    removed = True
            except Exception as e:
                _reraise_document_disposed(e, "Writer")
                log.debug("clear_writer_body: could not dispose %s %r", supplier, name, exc_info=True)
    try:
        page = doc.getDrawPage()
        # Bounded, never `while getCount()`: if a remove silently fails the count never
        # drops, and an unbounded loop here would freeze the main thread.
        # The cap is the count at entry, not a live getCount() check.
        removal_budget = int(page.getCount())
        for i in range(removal_budget - 1, -1, -1):
            try:
                before = page.getCount()
                page.remove(page.getByIndex(i))
                if page.getCount() < before:
                    removed = True
            except Exception as e:
                _reraise_document_disposed(e, "Writer")
                continue
    except Exception as e:
        _reraise_document_disposed(e, "Writer")
        log.debug("clear_writer_body: could not empty the draw page", exc_info=True)
    try:
        text = doc.getText()
        if (text.getString() or "").strip():
            removed = True
        text.setString("")
    except Exception as e:
        _reraise_document_disposed(e, "Writer")
        log.debug("clear_writer_body failed", exc_info=True)
    if removed:
        log.debug("clear_writer_body: dropped default-template content from a scratch Writer")
    return removed


def _get_clear_writer_body_fn() -> Any:
    import sys

    uno_ctx_mod = sys.modules.get("plugin.framework.uno_context")
    if uno_ctx_mod is not None:
        fn = getattr(uno_ctx_mod, "clear_writer_body", None)
        if fn is not None:
            return fn
    return clear_writer_body


def new_blank_writer(ctx: Any = None, *, target: str = "_blank", flags: int = 0, extra_props: tuple[Any, ...] = ()) -> Any:
    """Hidden, **empty** Writer used as a scratch buffer.

    ``private:factory/swriter`` is the only way to get a Writer with the
    user's own styles, and it honours the user's default template. A firm
    that sets its petition model as that template would otherwise get the
    model's text in every scratch doc. Callers append to it and read the
    whole body back, so the model's header ("AO DOUTO JUIZO DO ...") comes
    back glued to the real content and lands in range reads, plain-text
    conversions, and full-document rewrites. Empty the body before the
    caller sees the document.

    Returns None when the desktop is unavailable (no-VCL helper processes).
    """
    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    if desktop is None:
        return None
    import uno

    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    doc = desktop.loadComponentFromURL("private:factory/swriter", target, flags, (hidden,) + tuple(extra_props))
    if doc is None:
        return None
    # clear_writer_body logs and returns False on a non-disposal error.
    # An already-empty body is False too, so only a leftover non-empty
    # string is a failure. Returning the scratch Writer anyway would hand
    # the caller the default-template text this function exists to drop.
    # Disposal still raises.
    clear_fn = _get_clear_writer_body_fn()
    if not clear_fn(doc):
        try:
            leftover = doc.getText().getString()
        except Exception as e:
            _reraise_document_disposed(e, "Writer")
            log.debug("new_blank_writer: body unreadable after clear", exc_info=True)
            _close_scratch_doc(doc)
            return None
        if (leftover or "").strip():
            log.debug("new_blank_writer: default template text survived clear_writer_body")
            _close_scratch_doc(doc)
            return None
    # Other document lookups wrap the model so a later off-thread use is
    # caught by the dev thread guard. This factory used to return it raw.
    return _guard_returned_uno(doc)
