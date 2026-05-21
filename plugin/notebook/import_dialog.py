# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""File picker + import Jupyter notebook into active Writer document."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import uno

from plugin.chatbot.dialogs import msgbox
from plugin.contrib.nbformat import NBFormatError
from plugin.doc.document_helpers import is_writer
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_active_document, get_ctx
from plugin.notebook.writer_importer import flush_ui_idle, import_ipynb_to_writer

log = logging.getLogger("writeragent.notebook")


def _pick_ipynb_path(ctx: Any) -> str | None:
    smgr = ctx.getServiceManager()
    fp = smgr.createInstanceWithContext("com.sun.star.ui.dialogs.FilePicker", ctx)
    if fp is None:
        return None
    try:
        from com.sun.star.ui.dialogs.TemplateDescription import FILEOPEN_SIMPLE  # type: ignore

        fp.initialize((FILEOPEN_SIMPLE,))
    except Exception:
        fp.initialize(())
    fp.setTitle(_("Import Jupyter Notebook"))
    try:
        fp.appendFilter(_("Jupyter Notebook"), "*.ipynb")
        fp.appendFilter(_("All files"), "*.*")
        fp.setCurrentFilter(_("Jupyter Notebook"))
    except Exception:
        pass
    if fp.execute() != 1:
        return None
    files = fp.getFiles()
    if not files or len(files) < 1:
        return None
    return str(uno.fileUrlToSystemPath(files[0]))


def run_import_ipynb_dialog(uno_ctx: Any = None) -> None:
    """Menu handler: pick .ipynb and import as Writer form controls."""
    ctx = uno_ctx or get_ctx()
    doc = get_active_document(ctx)
    if doc is None:
        msgbox(ctx, "WriterAgent", _("Open a Writer document first."))
        return
    if not is_writer(doc):
        msgbox(ctx, "WriterAgent", _("Import Notebook is only supported in LibreOffice Writer."))
        return

    path = _pick_ipynb_path(ctx)
    if not path:
        log.debug("notebook import cancelled (no file selected)")
        return

    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = -1
    doc_url = ""
    try:
        if hasattr(doc, "getURL"):
            doc_url = str(doc.getURL() or "")
    except Exception:
        pass
    log.info(
        "notebook import dialog picked path=%s file_size_bytes=%d doc_url=%s",
        path,
        file_size,
        doc_url,
    )

    import_t0 = time.monotonic()
    try:
        stats = import_ipynb_to_writer(doc, path, ctx=ctx)
    except NBFormatError as e:
        msgbox(ctx, "WriterAgent", str(e))
        return
    except Exception as e:
        log.exception("Import notebook failed")
        msgbox(ctx, "WriterAgent", _("Failed to import notebook: {error}").format(error=str(e)))
        return

    log.info(
        "notebook import dialog finished elapsed_ms=%d stats=%s",
        int((time.monotonic() - import_t0) * 1000),
        stats,
    )
    flush_ui_idle(ctx)
    log.debug("notebook import showing completion message box")
    msgbox(
        ctx,
        "WriterAgent",
        _(
            "Imported notebook.\n"
            "Cells: {cells} (code: {code}, markdown: {markdown})\n"
            "Code input fields in document: {shapes}\n"
            "Output images: {images}"
        ).format(
            cells=stats["cells"],
            code=stats["code"],
            markdown=stats["markdown"],
            shapes=stats.get("shapes", stats.get("controls", 0)),
            images=stats.get("images", 0),
        ),
    )
    flush_ui_idle(ctx)
    log.debug("notebook import completion message box closed")
