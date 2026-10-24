# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.doc.doc_type."""

import types
from unittest.mock import patch

import pytest

from plugin.doc.doc_type import (
    CALC_DOCUMENT_SERVICE,
    DOCUMENT_SERVICES,
    DRAW_DOCUMENT_SERVICE,
    DocumentType,
    IMPRESS_DOCUMENT_SERVICE,
    TEXT_DOCUMENT_SERVICE,
    WEB_DOCUMENT_SERVICE,
    WRITER_DOCUMENT_SERVICE,
    agent_label_for_context,
    doc_type_family_label,
    doc_type_label_for_enum,
    doc_type_of,
    doc_type_title_for_label,
    get_document_type,
    get_document_uno_services,
    is_calc,
    is_draw,
    is_writer,
    uno_services_for_doc_type_label,
    uno_services_for_document,
)

_TEXT_SERVICE = TEXT_DOCUMENT_SERVICE
_CALC_SERVICE = CALC_DOCUMENT_SERVICE
_IMPRESS_SERVICE = IMPRESS_DOCUMENT_SERVICE
_DRAW_SERVICE = DRAW_DOCUMENT_SERVICE


def test_doc_type_label_and_title_helpers():
    assert doc_type_label_for_enum(DocumentType.WRITER) == "writer"
    assert doc_type_label_for_enum(DocumentType.CALC) == "calc"
    assert doc_type_label_for_enum(DocumentType.DRAW) == "draw"
    # ToolContext / sidebar: Impress stays distinct for PresentationDocument uno_services.
    assert doc_type_label_for_enum(DocumentType.IMPRESS) == "impress"
    # Document research / path-family labels collapse Draw+Impress to "draw".
    assert doc_type_family_label(DocumentType.IMPRESS) == "draw"
    assert doc_type_family_label(DocumentType.DRAW) == "draw"
    assert doc_type_label_for_enum(DocumentType.UNKNOWN) == "unknown"
    assert doc_type_family_label(DocumentType.UNKNOWN) == "unknown"
    # Direct assertion: Impress distinct label vs family label
    assert doc_type_label_for_enum(DocumentType.IMPRESS) == "impress"
    assert doc_type_family_label(DocumentType.IMPRESS) == "draw"
    assert doc_type_title_for_label("impress") == "Draw"
    assert doc_type_title_for_label("draw") == "Draw"
    assert doc_type_title_for_label("calc") == "Calc"


def test_agent_label_from_doc_type():
    def _ctx(doc_type: str, svc: str):
        return types.SimpleNamespace(
            doc_type=doc_type,
            uno_services_supported=(svc,) if svc else (),
        )

    assert agent_label_for_context(_ctx("writer", "com.sun.star.text.TextDocument")) == "Writer"
    assert agent_label_for_context(_ctx("calc", "com.sun.star.sheet.SpreadsheetDocument")) == "Calc"
    assert agent_label_for_context(_ctx("impress", "com.sun.star.presentation.PresentationDocument")) == "Draw"
    assert agent_label_for_context(_ctx("", "com.sun.star.sheet.SpreadsheetDocument")) == "Calc"
    assert agent_label_for_context(_ctx("", "")) == "Writer"


class _ServiceModel:
    def __init__(self, services: set[str]) -> None:
        self._services = services

    def supportsService(self, name: str) -> bool:
        return name in self._services


def test_get_document_type_impress_before_drawing_document():
    """Impress supports both PresentationDocument and DrawingDocument.

    Draw-only supports DrawingDocument only and stays DRAW. The Impress
    label is what the sidebar caches.
    """
    impress = _ServiceModel({_IMPRESS_SERVICE, _DRAW_SERVICE})
    draw_only = _ServiceModel({_DRAW_SERVICE})
    assert get_document_type(impress) == DocumentType.IMPRESS
    assert doc_type_label_for_enum(get_document_type(impress)) == "impress"
    assert get_document_type(draw_only) == DocumentType.DRAW
    assert doc_type_label_for_enum(get_document_type(draw_only)) == "draw"


@pytest.mark.parametrize(
    ("services", "expected_type", "expected_label", "expected_family", "expected_title"),
    [
        ({_TEXT_SERVICE}, DocumentType.WRITER, "writer", "writer", "Writer"),
        ({_CALC_SERVICE}, DocumentType.CALC, "calc", "calc", "Calc"),
        ({_IMPRESS_SERVICE}, DocumentType.IMPRESS, "impress", "draw", "Draw"),
        ({_IMPRESS_SERVICE, _DRAW_SERVICE}, DocumentType.IMPRESS, "impress", "draw", "Draw"),
        ({_DRAW_SERVICE}, DocumentType.DRAW, "draw", "draw", "Draw"),
        ({_TEXT_SERVICE, _CALC_SERVICE, _IMPRESS_SERVICE, _DRAW_SERVICE}, DocumentType.WRITER, "writer", "writer", "Writer"),
        (set(), DocumentType.UNKNOWN, "unknown", "unknown", None),
        (None, DocumentType.UNKNOWN, "unknown", "unknown", None),
    ],
)
def test_doc_type_truth_table(
    services: set[str] | None,
    expected_type: DocumentType,
    expected_label: str,
    expected_family: str,
    expected_title: str | None,
) -> None:
    model = _ServiceModel(services) if services is not None else None
    doc_type = get_document_type(model)
    assert doc_type == expected_type
    assert doc_type_label_for_enum(doc_type) == expected_label
    assert doc_type_family_label(doc_type) == expected_family
    if expected_title is not None:
        assert doc_type_title_for_label(expected_label) == expected_title

    assert is_writer(model) is (expected_type == DocumentType.WRITER)
    assert is_calc(model) is (expected_type == DocumentType.CALC)
    assert is_draw(model) is (expected_type in (DocumentType.DRAW, DocumentType.IMPRESS))


def test_doc_type_of_resolution() -> None:
    assert doc_type_of(None) is None
    assert doc_type_of(_ServiceModel({_TEXT_SERVICE})) == "writer"
    assert doc_type_of(_ServiceModel({_CALC_SERVICE})) == "calc"
    assert doc_type_of(_ServiceModel({_DRAW_SERVICE})) == "draw"

    class BrokenDoc:
        def supportsService(self, name: str) -> bool:
            raise RuntimeError("bridge disconnected")

    assert doc_type_of(BrokenDoc()) == "writer"



def test_get_document_uno_services_truth() -> None:
    impress_both = _ServiceModel({_IMPRESS_SERVICE, _DRAW_SERVICE})
    draw_only = _ServiceModel({_DRAW_SERVICE})
    assert get_document_uno_services(impress_both) == frozenset({_IMPRESS_SERVICE, _DRAW_SERVICE})
    assert get_document_uno_services(draw_only) == frozenset({_DRAW_SERVICE})


def test_uno_services_for_doc_type_label_truth() -> None:
    assert uno_services_for_doc_type_label("writer") == frozenset({_TEXT_SERVICE})
    assert uno_services_for_doc_type_label("calc") == frozenset({_CALC_SERVICE})
    assert uno_services_for_doc_type_label("draw") == frozenset({_DRAW_SERVICE, _IMPRESS_SERVICE})
    assert uno_services_for_doc_type_label("impress") == frozenset({_IMPRESS_SERVICE})
    assert uno_services_for_doc_type_label(None) == frozenset()
    assert uno_services_for_doc_type_label("") == frozenset()
    assert uno_services_for_doc_type_label("web") == frozenset()
    assert uno_services_for_doc_type_label("unknown") == frozenset()
    assert uno_services_for_doc_type_label(" Draw ") == frozenset({_DRAW_SERVICE, _IMPRESS_SERVICE})


def test_doc_type_title_for_unknown_labels() -> None:
    for label in (None, "", "web", "nope"):
        assert doc_type_title_for_label(label) == "Unknown"


def test_uno_services_for_document_fallback_and_off_main() -> None:
    calc_model = _ServiceModel({_CALC_SERVICE})
    empty_model = _ServiceModel(set())

    # On main thread with known service -> probed set
    assert uno_services_for_document(calc_model, "writer") == frozenset({_CALC_SERVICE})

    # On main thread with empty probe set -> label fallback
    assert uno_services_for_document(empty_model, "writer") == frozenset({_TEXT_SERVICE})

    # Off main thread -> uses label map instead of supportsService
    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
        assert uno_services_for_document(calc_model, "writer") == frozenset({_TEXT_SERVICE})


def test_document_service_constants() -> None:
    assert TEXT_DOCUMENT_SERVICE == "com.sun.star.text.TextDocument"
    assert WRITER_DOCUMENT_SERVICE == "com.sun.star.text.TextDocument"
    assert CALC_DOCUMENT_SERVICE == "com.sun.star.sheet.SpreadsheetDocument"
    assert DRAW_DOCUMENT_SERVICE == "com.sun.star.drawing.DrawingDocument"
    assert IMPRESS_DOCUMENT_SERVICE == "com.sun.star.presentation.PresentationDocument"
    assert WEB_DOCUMENT_SERVICE == "com.sun.star.text.WebDocument"
    assert DOCUMENT_SERVICES == [
        "com.sun.star.text.TextDocument",
        "com.sun.star.sheet.SpreadsheetDocument",
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]


