# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO coverage for nested Writer text-table relationships."""
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.tables import TableGetCells


@native_test
@with_native_doc("writer")
def test_table_get_cells_reports_nested_parent_relation_uno(ctx, doc):
    """A nested table reports its direct parent table/cell; a top-level table does not."""
    text = doc.getText()

    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    outer.setName("FixtureOuter")
    outer.getCellByName("A1").setString("OUTER_ALPHA")

    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(2, 2)
    host = outer.getCellByName("B2")
    host.insertTextContent(host.getStart(), nested, False)
    nested.setName("FixtureNested")
    nested.getCellByName("A1").setString("NESTED_ALPHA")
    nested.getCellByName("B2").setString("NESTED_OMEGA")

    standalone = doc.createInstance("com.sun.star.text.TextTable")
    standalone.initialize(1, 2)
    text.insertTextContent(text.getEnd(), standalone, False)
    standalone.setName("FixtureStandalone")
    standalone.getCellByName("A1").setString("STANDALONE_ALPHA")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    nested_res = TableGetCells().execute(tool_ctx, name="FixtureNested")
    assert nested_res.get("status") == "ok", nested_res
    assert nested_res["matrix"][0][0] == "NESTED_ALPHA"
    assert nested_res["matrix"][1][1] == "NESTED_OMEGA"
    assert nested_res["nesting"] == {
        "is_nested": True,
        "parent_table": "FixtureOuter",
        "parent_cell": "B2",
    }

    standalone_res = TableGetCells().execute(tool_ctx, name="FixtureStandalone")
    assert standalone_res.get("status") == "ok", standalone_res
    assert standalone_res["matrix"][0][0] == "STANDALONE_ALPHA"
    assert standalone_res["nesting"] == {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }
