# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native file import filter for Jupyter Notebooks (.ipynb) in Writer."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# --- Minimal stdlib-only bootstrap ---
_this = os.path.abspath(__file__)
for __ in range(3):  # plugin/notebook/import_filter.py → plugin/notebook/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(
    __file__,
    levels_up=3,
    also_add_plugin_dir=True,
    also_add_lib=True,
    also_add_vendor=True,
)

import uno  # noqa: E402
import unohelper  # noqa: E402
from com.sun.star.document import XFilter, XImporter  # noqa: E402
from com.sun.star.lang import XServiceInfo  # noqa: E402

from plugin.contrib.nbformat import NBFormatError  # noqa: E402
from plugin.notebook.writer_importer import import_ipynb_to_writer  # noqa: E402

log = logging.getLogger("writeragent.notebook")

IMPL_NAME = "org.extension.writeragent.JupyterNotebookImportFilter"


class JupyterNotebookImportFilter(unohelper.Base, XFilter, XImporter, XServiceInfo):
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.target_doc = None

    # XImporter
    def setTargetDocument(self, doc: Any) -> None:
        self.target_doc = doc

    # XFilter
    def filter(self, media_descriptor: Any) -> bool:
        file_url = ""
        for prop in media_descriptor:
            if prop.Name == "URL":
                file_url = prop.Value
                break

        if not file_url or not self.target_doc:
            return False

        try:
            file_path = uno.fileUrlToSystemPath(file_url)
            import_ipynb_to_writer(self.target_doc, file_path, ctx=self.ctx)
            return True
        except NBFormatError:
            log.exception("Notebook format error")
            return False
        except Exception:
            log.exception("Failed to import notebook")
            return False

    def cancel(self) -> None:
        pass

    # XServiceInfo
    def getImplementationName(self) -> str:
        return IMPL_NAME

    def supportsService(self, service_name: str) -> bool:
        return service_name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self) -> tuple[str]:
        return ("com.sun.star.document.ImportFilter",)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    JupyterNotebookImportFilter,
    IMPL_NAME,
    ("com.sun.star.document.ImportFilter",),
)
