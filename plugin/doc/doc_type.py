# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document type detection and sidebar/tool label maps (no Calc analyzer import).

Menu guards and ``=PY()`` session lookup need ``is_writer`` / ``is_calc`` without
loading ``document_helpers`` → ``SheetAnalyzer``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from plugin.framework.errors import safe_call, safe_uno_call
from plugin.framework.thread_guard import main_thread_only


class DocumentType(Enum):
    UNKNOWN = auto()
    WRITER = auto()
    CALC = auto()
    DRAW = auto()
    IMPRESS = auto()


# Canonical UNO service names for Writer/Calc/Draw/Impress (visual_helpers
# duplicates the strings plus WebDocument, which must be checked first).
_DOCUMENT_SERVICE_MAP = {
    DocumentType.WRITER: "com.sun.star.text.TextDocument",
    DocumentType.CALC: "com.sun.star.sheet.SpreadsheetDocument",
    DocumentType.DRAW: "com.sun.star.drawing.DrawingDocument",
    DocumentType.IMPRESS: "com.sun.star.presentation.PresentationDocument",
}

# Lowercase doc_type labels (ToolContext / sidebar) -> UNO services for tool compatibility
# without re-querying the live document. Impress is distinct; sidebar "Draw" covers both draw kinds.
_DOC_TYPE_LABEL_TO_UNO_SERVICES: dict[str, frozenset[str]] = {
    "writer": frozenset({"com.sun.star.text.TextDocument"}),
    "calc": frozenset({"com.sun.star.sheet.SpreadsheetDocument"}),
    "draw": frozenset({"com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"}),
    "impress": frozenset({"com.sun.star.presentation.PresentationDocument"}),
}


def uno_services_for_doc_type_label(doc_type: str | None) -> frozenset[str]:
    """Return UNO services implied by a sidebar/doc_type label (no live document)."""
    if not doc_type:
        return frozenset()
    return _DOC_TYPE_LABEL_TO_UNO_SERVICES.get(str(doc_type).strip().lower(), frozenset())


def uno_services_for_document(model: Any, doc_type: str | None) -> frozenset[str]:
    """Return UNO services for tool filtering: main-thread probe when possible, else doc_type map."""
    from plugin.framework.thread_guard import on_main_thread

    if model is not None and on_main_thread():
        try:
            services = get_document_uno_services(model)
            if services:
                return services
        except Exception:
            pass
    return uno_services_for_doc_type_label(doc_type)


# @main_thread_only MUST be outer so off-thread calls raise instead of
# being swallowed by @safe_uno_call (same order as get_document_type).
@main_thread_only
@safe_uno_call(default=frozenset())
def get_document_uno_services(model: Any) -> frozenset[str]:
    """Return UNO service names supported by *model* (main thread only; cache in sidebar/MCP)."""
    if model is None:
        return frozenset()
    found: set[str] = set()
    for _unused_doc_type, service_name in _DOCUMENT_SERVICE_MAP.items():
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            found.add(service_name)
    return frozenset(found)


def doc_type_label_for_enum(doc_type: DocumentType, *, impress_as_draw: bool = False) -> str:
    """Lowercase ToolContext / research doc_type label for a DocumentType enum value.

    Sidebar and tool filtering keep Impress distinct (``\"impress\"``) so
    ``uno_services`` can list PresentationDocument alone. Document research and
    some visual paths treat Draw+Impress as one family — pass
    ``impress_as_draw=True`` for that contract (label ``\"draw\"``).
    """
    if doc_type == DocumentType.CALC:
        return "calc"
    if doc_type == DocumentType.WRITER:
        return "writer"
    if doc_type == DocumentType.IMPRESS:
        return "draw" if impress_as_draw else "impress"
    if doc_type == DocumentType.DRAW:
        return "draw"
    return "unknown"


def doc_type_title_for_label(label: str | None) -> str:
    """Sidebar display title (Writer/Calc/Draw) for a cached lowercase doc_type label."""
    if not label:
        return "Unknown"
    return {
        "writer": "Writer",
        "calc": "Calc",
        "draw": "Draw",
        "impress": "Draw",
    }.get(str(label).strip().lower(), "Unknown")


# Bugfix: @main_thread_only MUST be the outer decorator here so off-main thread calls
# raise thread violation errors immediately instead of being swallowed by @safe_uno_call.
@main_thread_only
@safe_uno_call(default=DocumentType.UNKNOWN)
def get_document_type(model: Any) -> DocumentType:
    """Return the DocumentType for the given model."""
    if model is None:
        return DocumentType.UNKNOWN

    # Four supportsService calls; id(model) is reused after close so a cache
    # keyed that way would return the wrong type for a new document.
    # Check services in priority order
    for doc_type, service_name in _DOCUMENT_SERVICE_MAP.items():
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            return doc_type

    return DocumentType.UNKNOWN



def is_writer(model: Any) -> bool:
    """Return True if model is a Writer document."""
    return get_document_type(model) == DocumentType.WRITER


def is_calc(model: Any) -> bool:
    """Return True if model is a Calc document."""
    return get_document_type(model) == DocumentType.CALC


def is_draw(model: Any) -> bool:
    """Return True if model is a Draw/Impress document."""
    doc_type = get_document_type(model)
    return doc_type in (DocumentType.DRAW, DocumentType.IMPRESS)
