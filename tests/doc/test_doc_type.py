# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.doc.doc_type."""

from plugin.doc.doc_type import (
    DocumentType,
    doc_type_label_for_enum,
    doc_type_title_for_label,
    get_document_type,
)

_DRAW_SERVICE = "com.sun.star.drawing.DrawingDocument"
_IMPRESS_SERVICE = "com.sun.star.presentation.PresentationDocument"


def test_doc_type_label_and_title_helpers():
    assert doc_type_label_for_enum(DocumentType.WRITER) == "writer"
    assert doc_type_label_for_enum(DocumentType.CALC) == "calc"
    assert doc_type_label_for_enum(DocumentType.DRAW) == "draw"
    # ToolContext / sidebar: Impress stays distinct for PresentationDocument uno_services.
    assert doc_type_label_for_enum(DocumentType.IMPRESS) == "impress"
    # Document research / path-family labels collapse Draw+Impress to "draw".
    assert doc_type_label_for_enum(DocumentType.IMPRESS, impress_as_draw=True) == "draw"
    assert doc_type_label_for_enum(DocumentType.DRAW, impress_as_draw=True) == "draw"
    assert doc_type_label_for_enum(DocumentType.UNKNOWN) == "unknown"
    assert doc_type_title_for_label("impress") == "Draw"
    assert doc_type_title_for_label("draw") == "Draw"
    assert doc_type_title_for_label("calc") == "Calc"


class _ServiceModel:
    def __init__(self, services: set[str]) -> None:
        self._services = services

    def supportsService(self, name: str) -> bool:
        return name in self._services


def test_get_document_type_impress_before_drawing_document():
    """Impress supports both PresentationDocument and DrawingDocument.

    Draw-only supports DrawingDocument only and stays DRAW. The Impress
    label is what the sidebar caches (impress_as_draw=False).
    """
    impress = _ServiceModel({_IMPRESS_SERVICE, _DRAW_SERVICE})
    draw_only = _ServiceModel({_DRAW_SERVICE})
    assert get_document_type(impress) == DocumentType.IMPRESS
    assert doc_type_label_for_enum(get_document_type(impress), impress_as_draw=False) == "impress"
    assert get_document_type(draw_only) == DocumentType.DRAW
    assert doc_type_label_for_enum(get_document_type(draw_only)) == "draw"
