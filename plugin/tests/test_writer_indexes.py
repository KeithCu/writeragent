import sys
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

import pytest
from plugin.modules.writer.indexes import IndexesList, IndexesCreate, IndexesAddMark

def test_indexes_list():
    tool = IndexesList()
    ctx = MagicMock()
    doc = ctx.doc
    indexes_mock = MagicMock()
    doc.getDocumentIndexes.return_value = indexes_mock
    indexes_mock.getCount.return_value = 2

    idx1 = MagicMock()
    idx1.getName.return_value = "Index1"
    idx1.Title = "Title1"
    idx1.getImplementationName.return_value = "SwXContentIndex"

    idx2 = MagicMock()
    idx2.getName.return_value = "Index2"
    idx2.Title = "Title2"
    idx2.getImplementationName.return_value = "SwXDocumentIndex"

    indexes_mock.getByIndex.side_effect = [idx1, idx2]

    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["indexes"]) == 2
    assert res["indexes"][0]["name"] == "Index1"
    assert res["indexes"][0]["title"] == "Title1"
    assert res["indexes"][0]["type"] == "toc"
    assert res["indexes"][1]["name"] == "Index2"
    assert res["indexes"][1]["title"] == "Title2"
    assert res["indexes"][1]["type"] == "alphabetical"

def test_indexes_create():
    tool = IndexesCreate()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock

    index_mock = MagicMock()
    doc.createInstance.return_value = index_mock

    res = tool.execute(ctx, index_kind="toc", title="My TOC", create_from_outline=True, target="beginning")
    assert res["status"] == "ok"
    assert res["title"] == "My TOC"

    doc.createInstance.assert_called_with("com.sun.star.text.ContentIndex")
    assert index_mock.Title == "My TOC"
    assert index_mock.CreateFromOutline == True

    text_mock = cursor_mock.getText()
    text_mock.insertTextContent.assert_called_with(cursor_mock, index_mock, False)
    index_mock.update.assert_called()

def test_indexes_add_mark():
    tool = IndexesAddMark()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock

    mark_mock = MagicMock()
    doc.createInstance.return_value = mark_mock

    res = tool.execute(ctx, mark_text="Important Term", index_kind="alphabetical", primary_key="Terms", target="beginning")
    assert res["status"] == "ok"
    assert res["message"] == "Added 'alphabetical' index mark for 'Important Term'"

    doc.createInstance.assert_called_with("com.sun.star.text.DocumentIndexMark")
    assert mark_mock.MarkEntry == "Important Term"
    assert mark_mock.PrimaryKey == "Terms"

    text_mock = cursor_mock.getText()
    text_mock.insertTextContent.assert_called_with(cursor_mock, mark_mock, False)
