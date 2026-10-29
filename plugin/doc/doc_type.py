# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document type detection (no Calc analyzer import).

Probe order is Writer, Calc, Impress, Draw. Impress also supports
DrawingDocument, so it is tested before Draw. Impress's distinct label
is ``impress``; family and display title collapse to ``draw`` / ``Draw``.
Menu guards and ``=PY()`` need ``is_writer`` / ``is_calc`` without importing
``document_helpers`` (that imports ``SheetAnalyzer``).
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Any, Literal

from plugin.framework.errors import best_effort, safe_call, safe_uno_call
from plugin.framework.thread_guard import main_thread_only

DocTypeLabel = Literal["writer", "calc", "draw", "impress", "unknown"]
DocTypeFamily = Literal["writer", "calc", "draw", "unknown"]

TEXT_DOCUMENT_SERVICE = "com.sun.star.text.TextDocument"
CALC_DOCUMENT_SERVICE = "com.sun.star.sheet.SpreadsheetDocument"
DRAW_DOCUMENT_SERVICE = "com.sun.star.drawing.DrawingDocument"
IMPRESS_DOCUMENT_SERVICE = "com.sun.star.presentation.PresentationDocument"
WRITER_DOCUMENT_SERVICE = TEXT_DOCUMENT_SERVICE
WEB_DOCUMENT_SERVICE = "com.sun.star.text.WebDocument"

DOCUMENT_SERVICES: list[str] = [
    TEXT_DOCUMENT_SERVICE,
    CALC_DOCUMENT_SERVICE,
    DRAW_DOCUMENT_SERVICE,
    IMPRESS_DOCUMENT_SERVICE,
]


class DocumentType(Enum):
    UNKNOWN = auto()
    WRITER = auto()
    CALC = auto()
    DRAW = auto()
    IMPRESS = auto()


# (service, distinct label). Family is "draw" only for label "impress".
_TYPE_ROWS: dict[DocumentType, tuple[str, DocTypeLabel]] = {
    DocumentType.WRITER: (TEXT_DOCUMENT_SERVICE, "writer"),
    DocumentType.CALC: (CALC_DOCUMENT_SERVICE, "calc"),
    DocumentType.IMPRESS: (IMPRESS_DOCUMENT_SERVICE, "impress"),
    DocumentType.DRAW: (DRAW_DOCUMENT_SERVICE, "draw"),
}

_SERVICE_PRIORITY: tuple[DocumentType, ...] = (
    DocumentType.WRITER,
    DocumentType.CALC,
    DocumentType.IMPRESS,
    DocumentType.DRAW,
)

# "impress" is PresentationDocument only. "draw" is the family (both services).
_LABEL_TO_UNO_SERVICES: dict[str, frozenset[str]] = {
    "writer": frozenset((TEXT_DOCUMENT_SERVICE,)),
    "calc": frozenset((CALC_DOCUMENT_SERVICE,)),
    "impress": frozenset((IMPRESS_DOCUMENT_SERVICE,)),
    "draw": frozenset((DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE)),
}

_log = logging.getLogger(__name__)


def uno_services_for_doc_type_label(doc_type: str | None) -> frozenset[str]:
    """Return UNO services implied by a sidebar/doc_type label (no live document)."""
    return _LABEL_TO_UNO_SERVICES.get((doc_type or "").strip().lower(), frozenset())


def uno_services_for_document(model: Any, doc_type: str | None) -> frozenset[str]:
    """Return UNO services for tool filtering: main-thread probe when possible, else doc_type map."""
    from plugin.framework.thread_guard import on_main_thread

    if model is not None and on_main_thread():
        services = get_document_uno_services(model)
        if services:
            return services
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
    for dt in _SERVICE_PRIORITY:
        service_name = _TYPE_ROWS[dt][0]
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            found.add(service_name)
    return frozenset(found)


def doc_type_label_for_enum(doc_type: DocumentType) -> DocTypeLabel:
    """Distinct lowercase label (``\"writer\"``, ``\"calc\"``, ``\"draw\"``, ``\"impress\"``, ``\"unknown\"``)."""
    row = _TYPE_ROWS.get(doc_type)
    return row[1] if row is not None else "unknown"


def doc_type_family_label(doc_type: DocumentType) -> DocTypeFamily:
    """Research / path-family label folding Impress into ``\"draw\"``."""
    label = doc_type_label_for_enum(doc_type)
    if label == "impress":
        return "draw"
    return label


def doc_type_str_from_doc(doc: Any) -> str | None:
    """Map UNO document model to tool context doc_type string."""
    # crosshair: off
    if doc is None:
        return None
    try:
        dt = get_document_type(doc)
        label = doc_type_family_label(dt)
        return None if label == "unknown" else label
    except Exception:
        _log.exception("doc_type probe failed")
    return None


def _display_title(label: str | None) -> str | None:
    """Writer, Calc, or Draw. None if empty or unknown. impress displays as Draw."""
    norm = (label or "").strip().lower()
    if norm == "impress":
        norm = "draw"
    if norm in ("writer", "calc", "draw"):
        return norm.capitalize()
    return None


def doc_type_title_for_label(label: str | None) -> str:
    """Sidebar display title (Writer/Calc/Draw) for a cached lowercase doc_type label."""
    return _display_title(label) or "Unknown"


def agent_label_for_doc_type(doc_type: str | None) -> str | None:
    """Sidebar / agent display label (Writer, Calc, Draw) or None for unknown/empty."""
    return _display_title(doc_type)


def agent_label_for_context(ctx: Any) -> str:
    """Writer / Calc / Draw label used to validate specialized domain names."""
    raw = (getattr(ctx, "doc_type", None) or "").strip().lower()
    titled = _display_title(raw)
    if titled is not None:
        return titled
    services = getattr(ctx, "uno_services_supported", None) or ()
    blob = " ".join(str(svc) for svc in services)
    if "SpreadsheetDocument" in blob:
        return "Calc"
    if "PresentationDocument" in blob or "DrawingDocument" in blob:
        return "Draw"
    return "Writer"


# @main_thread_only MUST be the outer decorator here so off-main
# thread calls raise thread violation errors immediately instead
# of being swallowed by @safe_uno_call.
@main_thread_only
@safe_uno_call(default=DocumentType.UNKNOWN)
def get_document_type(model: Any) -> DocumentType:
    """Return the DocumentType for the given model."""
    if model is None:
        return DocumentType.UNKNOWN

    # Four supportsService calls; id(model) is reused after close so a cache
    # keyed that way would return the wrong type for a new document.
    # Check services in priority order
    for dt in _SERVICE_PRIORITY:
        service_name = _TYPE_ROWS[dt][0]
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            return dt

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


def doc_type_of(doc: Any) -> str | None:
    """'writer' / 'calc' / 'draw' for a document model, or None when there is no document.

    Same resolution every other tool relies on (lazy import of is_calc / is_draw)."""
    if doc is None:
        return None

    def _classify() -> str:
        if is_calc(doc):
            return "calc"
        if is_draw(doc):
            return "draw"
        return "writer"

    return best_effort(_classify, "doc_type_of: could not classify document; defaulting to writer", "writer")

