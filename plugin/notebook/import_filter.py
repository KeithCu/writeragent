# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native file import filter for Jupyter Notebooks (.ipynb) in Writer."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, TYPE_CHECKING

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

if TYPE_CHECKING:
    from com.sun.star.document import XFilter, XImporter, XExtendedFilterDetection
    from com.sun.star.lang import XServiceInfo
else:
    # Pytest collection uses venv stubs: com.sun.star.document exists but has
    # no XFilter. LibreOffice Python has the real IDL.
    try:
        from com.sun.star.document import XFilter, XImporter, XExtendedFilterDetection
        from com.sun.star.lang import XServiceInfo
    except ImportError:
        class XFilter:
            pass

        class XImporter:
            pass

        class XExtendedFilterDetection:
            pass

        class XServiceInfo:
            pass

from plugin.contrib.nbformat import NBFormatError  # noqa: E402
from plugin.notebook.writer_importer import import_ipynb_to_writer  # noqa: E402

log = logging.getLogger("writeragent.notebook")

IMPL_NAME = "org.extension.writeragent.JupyterNotebookImportFilter"


class JupyterNotebookImportFilter(unohelper.Base, XFilter, XImporter, XServiceInfo, XExtendedFilterDetection):
    ctx: Any
    target_doc: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.target_doc = None

    # XExtendedFilterDetection — param names must match IDL stubs (ty Liskov).
    # PyUNO requires (typeName, descriptor), not a bare string (#482).
    def detect(self, Descriptor: Any) -> Any:  # type: ignore
        file_url = ""
        props = list(Descriptor or ())
        for prop in props:
            if getattr(prop, "Name", "") == "URL":
                file_url = str(prop.Value or "")
                break

        type_name = ""
        if file_url.lower().endswith(".ipynb"):
            type_name = "writer_WriterAgent_Jupyter_Notebook"
        return type_name, tuple(props)

    # XImporter
    def setTargetDocument(self, Document: Any) -> None:
        self.target_doc = Document

    # XFilter
    def filter(self, aDescriptor: Any) -> bool:
        file_url = ""
        for prop in aDescriptor:
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

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self.getSupportedServiceNames()

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return (
            "com.sun.star.document.ImportFilter",
            "com.sun.star.document.ExtendedTypeDetection",
        )


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    JupyterNotebookImportFilter,
    IMPL_NAME,
    (
        "com.sun.star.document.ImportFilter",
        "com.sun.star.document.ExtendedTypeDetection",
    ),
)
