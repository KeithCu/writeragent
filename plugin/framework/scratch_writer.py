# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden scratch documents, with their creation, registry and cleanup.

Split out from ``uno_context`` to separate scratch document lifecycle
from core context and desktop lookup.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.errors import _uno_disposal_message, reraise_if_disposed
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import create_property_value, get_desktop, get_runtime_uid

log = logging.getLogger("writeragent.scratch_writer")

# _scratch_lock guards the three names below it.
# Scratch documents are excluded from DocumentService.open_documents: counting one broadens
# tools/list, fires a false tools/list_changed, and breaks single-open-document fallback
# (NO_ACTIVE_DOCUMENT). Keyed by RuntimeUID rather than Hidden because user-hidden documents
# must still count, and LibreOffice never reuses a uid in a process so stale entries are safe.
_scratch_lock = threading.Lock()
_scratch_uids: set[str] = set()
_scratch_loads = 0
_after_loads: Callable[[], object] | None = None


def load_scratch_document(
    desktop: Any,
    url: str,
    target: str = "_blank",
    flags: int = 0,
    props: tuple[Any, ...] = (),
) -> Any:
    """Load *url* hidden as a WriterAgent scratch document and register its RuntimeUID.

    Returns what ``loadComponentFromURL`` returned (``None`` included).
    """
    global _scratch_loads, _after_loads
    with _scratch_lock:
        _scratch_loads += 1
    try:
        doc = desktop.loadComponentFromURL(
            url, target, flags, (create_property_value("Hidden", True), *props)
        )
        uid = get_runtime_uid(doc) if doc is not None else ""
        if uid:
            with _scratch_lock:
                _scratch_uids.add(uid)
        return doc
    finally:
        deferred = None
        with _scratch_lock:
            _scratch_loads -= 1
            if _scratch_loads == 0:
                deferred, _after_loads = _after_loads, None
        if deferred is not None:
            deferred()


def close_scratch_document(doc: Any) -> None:
    """``close(True)``, then unregister.

    A failed close raises and leaves the id registered, because the document may still be open.
    """
    uid = get_runtime_uid(doc)  # read first; a closed model cannot answer
    doc.close(True)
    with _scratch_lock:
        _scratch_uids.discard(uid)


def is_scratch_document(doc: Any) -> bool:
    """Return True when *doc* is a currently registered WriterAgent scratch document."""
    uid = get_runtime_uid(doc)
    with _scratch_lock:
        return uid in _scratch_uids


def run_after_scratch_loads(report: Callable[[], object]) -> None:
    """Run *report* now, or once when the scratch loads in progress finish.

    ``OnViewCreated`` is broadcast inside ``loadComponentFromURL`` when the model is already
    enumerated on the desktop, so an immediate report would count it before its ``RuntimeUID``
    can be registered (``sfxbasemodel.cxx`` assigns the uid; the caller sees the model only
    when the load returns). One pending report is kept: there is one catalog listener and
    the report reads current state.
    """
    global _after_loads
    with _scratch_lock:
        if _scratch_loads:
            _after_loads = report
            return
    report()


def _close_scratch_doc(doc: Any) -> None:
    """Close a hidden scratch document we are about to abandon, so it does not leak."""
    try:
        close_scratch_document(doc)
    except Exception:
        log.debug("new_blank_writer: doc.close() failed", exc_info=True)


@main_thread_only
def clear_writer_body(doc: Any) -> bool:
    """Empty *doc* of everything a template can put in it. True when the doc is clean afterwards.

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
            reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
            continue
        for name in names:
            try:
                if container.hasByName(name):  # a nested table goes with its parent
                    container.getByName(name).dispose()
                    removed = True
            except Exception as e:
                reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
                log.debug("clear_writer_body: could not dispose %s %r", supplier, name, exc_info=True)
    try:
        page = doc.getDrawPage()
        # Bounded, never `while getCount()`: if a remove silently fails the count never
        # drops, and an unbounded loop here would freeze the main thread.
        removal_budget = int(page.getCount())
        for i in range(removal_budget - 1, -1, -1):
            try:
                before = page.getCount()
                page.remove(page.getByIndex(i))
                if page.getCount() < before:
                    removed = True
            except Exception as e:
                reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
                continue
    except Exception as e:
        reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
        log.debug("clear_writer_body: could not empty the draw page", exc_info=True)
    try:
        text = doc.getText()
        if (text.getString() or "").strip():
            removed = True
        text.setString("")
    except Exception as e:
        reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
        log.debug("clear_writer_body failed", exc_info=True)
    if removed:
        log.debug("clear_writer_body: dropped default-template content from a scratch Writer")

    try:
        return (
            doc.getTextTables().getCount() == 0
            and doc.getTextFrames().getCount() == 0
            and doc.getDrawPage().getCount() == 0
            and not (doc.getText().getString() or "").strip()
        )
    except Exception as e:
        reraise_if_disposed(e, _uno_disposal_message(e), object_type="Writer")
        return False


def new_blank_writer(ctx: Any = None, *, target: str = "_blank") -> Any:
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
    desktop = get_desktop(ctx)
    if desktop is None:
        return None

    doc = load_scratch_document(desktop, "private:factory/swriter", target)
    if doc is None:
        return None
    if not clear_writer_body(doc):
        log.debug("new_blank_writer: template content survived clear_writer_body")
        _close_scratch_doc(doc)
        return None
    # Other document lookups wrap the model so a later off-thread use is
    # caught by the dev thread guard.
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(doc)
