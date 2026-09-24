# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Regression test: apply_document_content(target='search') editing text inside a
# table cell. replace_preserving_format built its cursor on the document body text
# (model.getText()) instead of the matched range's own XText (the cell), raising the
# UNO RuntimeException "End of content node doesn't have the proper start node" and
# leaving the cell uneditable. The fix uses target_range.getText(), so the cursor
# resolves to the cell.
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.writer.content import ApplyDocumentContent
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)


@native_test
@with_native_doc("writer")
def test_apply_document_content_edits_table_cell_uno(ctx, doc):
    """Editing a cell's text via target='search' should work; it used to raise a
    cursor RuntimeException (body XText vs the cell's XText)."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(3, 2)
    text.insertTextContent(text.createTextCursor(), tbl, False)
    tbl.getCellByName("A2").setString("MinerU")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # plain-text content -> format-preserving path (where the bug lived).
    res = ApplyDocumentContent().execute(
        tool_ctx, content=["MinerU-EDIT"], old_content="MinerU", target="search"
    )
    assert res.get("status") == "ok", f"expected to edit the cell; got {res}"
    assert "MinerU-EDIT" in tbl.getCellByName("A2").getString()


@native_test
@with_native_doc("writer")
def test_apply_document_content_refuses_host_cell_with_nested_table_uno(ctx, doc):
    """Rewriting a host cell via apply_document_content would wipe the nested table."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    text = doc.getText()
    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(2, 2)
    text.insertTextContent(text.getEnd(), outer, False)
    host = outer.getCellByName("B2")
    host.setString("HOST_CAPTION")
    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    host.insertTextContent(host.getEnd(), nested, False)
    nested.setName("ApplyNested")
    nested.getCellByName("A1").setString("KEEP_INNER")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=["WIPED"], old_content="HOST_CAPTION", target="search"
    )
    assert res.get("status") == "error", res
    assert "table_set_cell" in (res.get("message") or ""), res
    assert doc.getTextTables().hasByName("ApplyNested")
    assert nested.getCellByName("A1").getString() == "KEEP_INNER"


def _insert_table(doc, name, rows, cols, cells):
    """Insert a named text table and return it. *cells* maps cell name -> string."""
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(rows, cols)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName(name)
    for cell_name, value in cells.items():
        table.getCellByName(cell_name).setString(value)
    return table


def _accept_all(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


def _reject_all(doc, ctx):
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:RejectAllTrackedChanges", "", 0, ())


@native_test
@with_native_doc("writer")
def test_empty_one_cell_leaves_the_table_uno(ctx, doc):
    """Clearing one cell while a sibling still has text is a text edit, not a table delete."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    table = _insert_table(doc, "Fee", 2, 2, {"A1": "Custas", "B1": "Honorarios"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=[""], old_content="Custas", target="search"
    )
    assert res.get("status") == "ok", res
    assert "table_deleted" not in res
    assert "table_still_present" not in res
    assert doc.getTextTables().hasByName("Fee")
    assert table.getCellByName("A1").getString().strip() == ""
    assert "Honorarios" in table.getCellByName("B1").getString()


@native_test
@with_native_doc("writer")
def test_empty_replacement_of_the_last_text_deletes_the_table_uno(ctx, doc):
    """The office failure: blanking the table's only text used to leave the shell and report success."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    text = doc.getText()
    _insert_table(doc, "Fee", 2, 2, {"A1": "ONLY_FEE"})
    text.insertString(text.getEnd(), "KEEP_OUTSIDE", False)
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=[""], old_content="ONLY_FEE", target="search"
    )
    assert res.get("status") == "ok", res
    assert res.get("table_deleted") == "Fee", res
    assert doc.getTextTables().hasByName("Fee") is False
    assert "KEEP_OUTSIDE" in text.getString()
    assert "ONLY_FEE" not in text.getString()


@native_test
@with_native_doc("writer")
def test_substring_clear_inside_a_cell_keeps_the_table_uno(ctx, doc):
    """Deleting part of a cell is not deleting the table, even when the other cells are empty."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    table = _insert_table(doc, "Fee", 1, 1, {"A1": "Custas processuais"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=[""], old_content="Custas", target="search"
    )
    assert res.get("status") == "ok", res
    assert "table_deleted" not in res
    assert doc.getTextTables().hasByName("Fee")
    leftover = table.getCellByName("A1").getString()
    assert "processuais" in leftover
    assert "Custas" not in leftover


@native_test
@with_native_doc("writer")
def test_emptying_an_inner_table_deletes_only_the_inner_table_uno(ctx, doc):
    """The inner table's last text deletes that table. The outer table and its other cell stay."""
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    outer = _insert_table(doc, "Outer", 2, 2, {"A1": "KEEP_HOST"})
    host = outer.getCellByName("B2")
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(1, 1)
    host.insertTextContent(host.getEnd(), inner, False)
    inner.setName("Inner")
    inner.getCellByName("A1").setString("INNER_ONLY")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, content=[""], old_content="INNER_ONLY", target="search"
    )
    assert res.get("status") == "ok", res
    assert res.get("table_deleted") == "Inner", res
    assert doc.getTextTables().hasByName("Inner") is False
    assert doc.getTextTables().hasByName("Outer")
    assert "KEEP_HOST" in outer.getCellByName("A1").getString()


@native_test
@with_native_doc("writer")
def test_emptied_table_stays_until_review_accept_uno(ctx, doc):
    """Under review the delete is a tracked change. removeTextContent would drop the table with no redline."""
    from plugin.framework.config import get_config, set_config

    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    _insert_table(doc, "Fee", 2, 2, {"A1": "ONLY_FEE"})
    before = doc.getRedlines().getCount()
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    set_config(flag, "record")
    try:
        res = ApplyDocumentContent().execute(
            tool_ctx, content=[""], old_content="ONLY_FEE", target="search"
        )
        assert res.get("status") == "ok", res
        assert res.get("table_deleted") == "Fee", res
        assert res.get("pending_review") is True, res
        assert doc.getTextTables().hasByName("Fee"), "the table stays until the user accepts"
        assert doc.getRedlines().getCount() > before
        _accept_all(doc, ctx)
        assert doc.getTextTables().hasByName("Fee") is False
    finally:
        set_config(flag, prev)


@native_test
@with_native_doc("writer")
def test_rejecting_the_emptied_table_restores_it_uno(ctx, doc):
    """Rejecting the tracked delete puts the table and its text back."""
    from plugin.framework.config import get_config, set_config

    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    _insert_table(doc, "Fee", 2, 2, {"A1": "ONLY_FEE"})
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    set_config(flag, "record")
    try:
        res = ApplyDocumentContent().execute(
            tool_ctx, content=[""], old_content="ONLY_FEE", target="search"
        )
        assert res.get("status") == "ok" and res.get("pending_review") is True, res
        _reject_all(doc, ctx)
        assert doc.getTextTables().hasByName("Fee")
        assert "ONLY_FEE" in doc.getTextTables().getByName("Fee").getCellByName("A1").getString()
    finally:
        set_config(flag, prev)
