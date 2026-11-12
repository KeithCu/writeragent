# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for document research MCP helpers (no LibreOffice required)."""

from unittest.mock import MagicMock, patch

from plugin.doc.document_research_tools import (
    ListOpenDocuments,
)
from plugin.writer.specialized.fields import (
    DocumentPropertiesGet,
    DocumentPropertiesSet,
)
from tests.writer.test_document_helpers import _DocWithUserDefinedProperties, _UserDefinedProperties


def test_list_open_documents_does_not_require_a_document():
    # Live finding (2026-06-28): the MCP no-document gate blocks tools whose `requires_document`
    # is the default True. list_open_documents must work with no document open.
    assert ListOpenDocuments.requires_document is False


def test_document_properties_get_contract():
    tool = DocumentPropertiesGet()
    assert tool.tier == "specialized"
    assert tool.specialized_domain == "fields"
    assert tool.uno_services is None  # Works for all document types
    assert tool.requires_document is True
    assert tool.detects_mutation() is False


def test_document_properties_set_contract():
    tool = DocumentPropertiesSet()
    assert tool.tier == "specialized"
    assert tool.specialized_domain == "fields"
    assert tool.uno_services is None  # Works for all document types
    assert tool.requires_document is True
    assert tool.detects_mutation() is True


def test_document_properties_get_hides_product_udprops():
    props = _UserDefinedProperties()
    props.values["Visible"] = "yes"
    props.values["WriterAgentGrammarCache"] = "secret"
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        def __init__(self):
            self.UserDefinedProperties = props
            self.Title = "T"
            self.Subject = "S"
            self.Author = "A"
            self.Description = "D"
            self.Keywords = ("k1",)

    doc.getDocumentProperties = lambda: _DocProps()

    ctx = MagicMock(doc=doc)
    with patch("plugin.framework.queue_executor.main.run", side_effect=lambda fn: fn()):
        res = DocumentPropertiesGet().execute(ctx)
    assert res["status"] == "ok"
    assert res["title"] == "T"
    assert res["user_defined"] == {"Visible": "yes"}
    assert "WriterAgentGrammarCache" in res["internal_omitted"]
    assert "WriterAgentGrammarCache" not in res["user_defined"]


def test_document_properties_set_writes_and_rejects_internal():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        def __init__(self):
            self.UserDefinedProperties = props
            self.Title = ""
            self.Subject = ""
            self.Author = ""
            self.Description = ""
            self.Keywords = ()

    bag = _DocProps()
    doc.getDocumentProperties = lambda: bag

    ctx = MagicMock(doc=doc)
    with patch("plugin.framework.queue_executor.main.run", side_effect=lambda fn: fn()):
        res = DocumentPropertiesSet().execute(ctx, title="New title", user_defined={"Custom": "v"})
    assert res["status"] == "ok"
    assert bag.Title == "New title"
    assert props.values["Custom"] == "v"
    assert "title" in res["updated"]

    bad = DocumentPropertiesSet().execute(ctx, user_defined={"WriterAgentSecret": "nope"})
    assert bad["status"] == "error"
    assert bad["code"] == "INVALID_PARAM"


def test_document_properties_set_no_changes_and_conflicts():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        UserDefinedProperties = props
        Title = Subject = Author = Description = ""
        Keywords = ()

    doc.getDocumentProperties = lambda: _DocProps()
    ctx = MagicMock(doc=doc)

    empty = DocumentPropertiesSet().execute(ctx)
    assert empty["code"] == "NO_CHANGES"

    conflict = DocumentPropertiesSet().execute(
        ctx,
        user_defined={"Same": "a"},
        remove_user_defined=["Same"],
    )
    assert conflict["code"] == "INVALID_PARAM"

    bad_kw = DocumentPropertiesSet().execute(ctx, keywords="one,two")
    assert bad_kw["code"] == "INVALID_PARAM"

    bad_kw_item = DocumentPropertiesSet().execute(ctx, keywords=["ok", 1])
    assert bad_kw_item["code"] == "INVALID_PARAM"


def test_document_properties_set_partial_updated_on_ud_failure():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        def __init__(self):
            self.UserDefinedProperties = props
            self.Title = ""
            self.Subject = ""
            self.Author = ""
            self.Description = ""
            self.Keywords = ()

    bag = _DocProps()
    doc.getDocumentProperties = lambda: bag
    ctx = MagicMock(doc=doc)
    with (
        patch("plugin.framework.queue_executor.main.run", side_effect=lambda fn: fn()),
        patch(
            "plugin.doc.udprops.set_document_property",
            side_effect=lambda _doc, key, _val: key != "bad",
        ),
    ):
        res = DocumentPropertiesSet().execute(
            ctx,
            title="Saved title",
            user_defined={"good": "v", "bad": "x"},
        )
    assert res["status"] == "error"
    assert res["code"] == "SET_PROPERTY_FAILED"
    assert bag.Title == "Saved title"
    assert res.get("details", {}).get("updated") == ["title", "user_defined:good"]


def test_document_properties_set_keywords_array():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)

    class _DocProps:
        UserDefinedProperties = props
        Title = Subject = Author = Description = ""
        Keywords = ()

    bag = _DocProps()
    doc.getDocumentProperties = lambda: bag
    ctx = MagicMock(doc=doc)
    with patch("plugin.framework.queue_executor.main.run", side_effect=lambda fn: fn()):
        res = DocumentPropertiesSet().execute(ctx, keywords=["a", "b"])
    assert res["status"] == "ok"
    assert bag.Keywords == ("a", "b")
