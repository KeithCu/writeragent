# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document type detection and sidebar/tool label maps (no Calc analyzer import).

Four vocabularies are coordinated by ``_META``:
1. Canonical UNO service (e.g. ``com.sun.star.text.TextDocument``).
2. Distinct lowercase label (``writer``, ``calc``, ``draw``, ``impress``, ``unknown``)
   via ``doc_type_label_for_enum``.
3. Family lowercase label (``writer``, ``calc``, ``draw``, ``unknown``)
   via ``doc_type_family_label``, where Impress collapses to ``draw``.
4. Display title (``Writer``, ``Calc``, ``Draw``, ``Unknown``)
   via ``doc_type_title_for_label``.

``_SERVICE_PRIORITY`` defines classification and probe priority (Writer, Calc,
Impress, Draw), which differs from enum declaration order because Impress
supports both PresentationDocument and DrawingDocument and must be tested first.

Menu guards and ``=PY()`` session lookup need ``is_writer`` / ``is_calc`` without
loading ``document_helpers`` → ``SheetAnalyzer``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Literal

from plugin.framework.errors import safe_call, safe_uno_call
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


@dataclass(frozen=True)
class _DocTypeMeta:
    service: str
    label: DocTypeLabel  # writer / calc / draw / impress
    family: DocTypeFamily  # writer / calc / draw; Impress family is draw
    display: str  # Writer / Calc / Draw


_META: dict[DocumentType, _DocTypeMeta] = {
    DocumentType.WRITER: _DocTypeMeta(TEXT_DOCUMENT_SERVICE, "writer", "writer", "Writer"),
    DocumentType.CALC: _DocTypeMeta(CALC_DOCUMENT_SERVICE, "calc", "calc", "Calc"),
    DocumentType.IMPRESS: _DocTypeMeta(IMPRESS_DOCUMENT_SERVICE, "impress", "draw", "Draw"),
    DocumentType.DRAW: _DocTypeMeta(DRAW_DOCUMENT_SERVICE, "draw", "draw", "Draw"),
}

_SERVICE_PRIORITY: tuple[DocumentType, ...] = (
    DocumentType.WRITER,
    DocumentType.CALC,
    DocumentType.IMPRESS,
    DocumentType.DRAW,
)


def _build_label_to_services() -> dict[str, frozenset[str]]:
    mapping: dict[str, set[str]] = {}
    for dt in _SERVICE_PRIORITY:
        meta = _META[dt]
        mapping.setdefault(meta.label, set()).add(meta.service)
        mapping.setdefault(meta.family, set()).add(meta.service)
    return {k: frozenset(v) for k, v in mapping.items()}


_LABEL_TO_UNO_SERVICES: dict[str, frozenset[str]] = _build_label_to_services()
_LABEL_TO_TITLE: dict[str, str] = {meta.label: meta.display for meta in _META.values()}


def _normalize_label(label: str | None) -> str:
    if not label:
        return ""
    return str(label).strip().lower()


def uno_services_for_doc_type_label(doc_type: str | None) -> frozenset[str]:
    """Return UNO services implied by a sidebar/doc_type label (no live document)."""
    norm = _normalize_label(doc_type)
    if not norm:
        return frozenset()
    return _LABEL_TO_UNO_SERVICES.get(norm, frozenset())


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
        service_name = _META[dt].service
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            found.add(service_name)
    return frozenset(found)


def doc_type_label_for_enum(doc_type: DocumentType) -> DocTypeLabel:
    """Distinct lowercase label (``\"writer\"``, ``\"calc\"``, ``\"draw\"``, ``\"impress\"``, ``\"unknown\"``)."""
    meta = _META.get(doc_type)
    return meta.label if meta is not None else "unknown"


def doc_type_family_label(doc_type: DocumentType) -> DocTypeFamily:
    """Research / path-family label folding Impress into ``\"draw\"``."""
    meta = _META.get(doc_type)
    return meta.family if meta is not None else "unknown"


def doc_type_title_for_label(label: str | None) -> str:
    """Sidebar display title (Writer/Calc/Draw) for a cached lowercase doc_type label."""
    norm = _normalize_label(label)
    if not norm:
        return "Unknown"
    return _LABEL_TO_TITLE.get(norm, "Unknown")


def agent_label_for_context(ctx: Any) -> str:
    """Writer / Calc / Draw label used to validate specialized domain names."""
    raw = (getattr(ctx, "doc_type", None) or "").strip().lower()
    titled = doc_type_title_for_label(raw)
    if raw and titled != "Unknown":
        return titled
    services = getattr(ctx, "uno_services_supported", None) or ()
    blob = " ".join(str(svc) for svc in services)
    if "SpreadsheetDocument" in blob:
        return "Calc"
    if "PresentationDocument" in blob or "DrawingDocument" in blob:
        return "Draw"
    if "TextDocument" in blob:
        return "Writer"
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
        service_name = _META[dt].service
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
