# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``resolve_document_by_url`` targeting by URL *or* RuntimeUID.

The RuntimeUID path lets a caller address a document that has no file URL yet
(unsaved/untitled), which the URL-only matcher could not reach. UNO is mocked
here; the live UNO path is exercised by the MCP integration run.
"""
from unittest.mock import MagicMock, patch

from plugin.doc.document_helpers import (
    DocumentType,
    get_runtime_uid,
    resolve_document_by_url,
)


def _model(url, uid):
    m = MagicMock()
    m.getURL.return_value = url
    m.RuntimeUID = uid
    return m


def _resolve(models, target, type_map=None):
    """Run resolve_document_by_url over *models* with UNO desktop/enumeration mocked."""
    type_map = type_map or {}
    desktop = MagicMock()

    def make_enum(*_a, **_k):
        pending = list(models)
        enum = MagicMock()
        enum.hasMoreElements.side_effect = lambda: len(pending) > 0
        enum.nextElement.side_effect = lambda: pending.pop(0)
        return enum

    desktop.getComponents.return_value.createEnumeration.side_effect = make_enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop), \
         patch("plugin.doc.document_helpers.get_document_type",
               side_effect=lambda m: type_map.get(m, DocumentType.WRITER)):
        return resolve_document_by_url(MagicMock(), target)


def test_match_by_url_regression():
    a = _model("file:///docs/a.odt", "uid-a")
    b = _model("file:///docs/b.ods", "uid-b")
    doc, doc_type = _resolve([a, b], "file:///docs/a.odt")
    assert doc is a
    assert doc_type == "writer"


def test_match_by_url_doc_type_calc():
    b = _model("file:///docs/b.ods", "uid-b")
    doc, doc_type = _resolve([b], "file:///docs/b.ods", type_map={b: DocumentType.CALC})
    assert doc is b
    assert doc_type == "calc"


def test_match_via_getController_getModel_branch():
    # An enumerated element that exposes getController().getModel() (a frame) instead of getURL
    # directly -- the desktop-enumeration path the URL/uid mocks otherwise skip (F4).
    inner = _model("file:///docs/c.odt", "uid-c")
    frame = MagicMock(spec=["getController"])  # no getURL attr -> forces the getController branch
    frame.getController.return_value.getModel.return_value = inner
    doc, doc_type = _resolve([frame], "file:///docs/c.odt")
    assert doc is inner
    assert doc_type == "writer"


def test_match_by_uid_when_target_is_not_a_url():
    a = _model("file:///docs/a.odt", "uid-a")
    b = _model("file:///docs/b.ods", "uid-b")
    doc, _ = _resolve([a, b], "uid-b")
    assert doc is b


def test_match_unsaved_doc_by_uid():
    # The whole point: an unsaved doc (no URL) is reachable only via its RuntimeUID.
    saved = _model("file:///docs/a.odt", "uid-a")
    untitled = _model("", "uid-untitled")
    doc, _ = _resolve([saved, untitled], "uid-untitled")
    assert doc is untitled


def test_no_match_returns_none():
    a = _model("file:///docs/a.odt", "uid-a")
    assert _resolve([a], "file:///docs/nope.odt") == (None, None)
    assert _resolve([a], "uid-does-not-exist") == (None, None)


def test_empty_inputs_never_match():
    # Empty target short-circuits; an empty uid/url must never match anything.
    assert resolve_document_by_url(MagicMock(), "") == (None, None)
    blank = _model("", "")
    assert _resolve([blank], "anything") == (None, None)


def test_get_runtime_uid_present_missing_and_error():
    present = type("M", (), {"RuntimeUID": "abc"})()
    assert get_runtime_uid(present) == "abc"

    missing = MagicMock(spec=[])  # no RuntimeUID attribute at all
    assert get_runtime_uid(missing) == ""

    class Boom:
        @property
        def RuntimeUID(self):  # e.g. a disposed UNO object
            raise RuntimeError("disposed")

    assert get_runtime_uid(Boom()) == ""
