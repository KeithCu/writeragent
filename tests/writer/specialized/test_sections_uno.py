import os
import tempfile
import zipfile

import uno

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.sections import (
    SectionCreate,
    SectionDelete,
    SectionSetColumns,
    SectionSetProtection,
)


def _fill_paragraphs(doc, count=6):
    text = doc.getText()
    cursor = text.createTextCursor()
    for idx in range(count):
        if idx > 0:
            text.insertControlCharacter(cursor, 0, False)
        text.insertString(cursor, f"Paragraph {idx} lorem ipsum dolor sit amet.", False)


def _paragraph_elements(doc):
    enum = doc.getText().createEnumeration()
    paras = []
    while enum.hasMoreElements():
        paras.append(enum.nextElement())
    return paras


def _save_odt_content_xml(doc):
    fd, path = tempfile.mkstemp(suffix=".odt")
    os.close(fd)
    try:
        prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
        prop.Name = "FilterName"
        prop.Value = "writer8"
        doc.storeToURL(uno.systemPathToFileUrl(path), (prop,))
        with zipfile.ZipFile(path) as zf:
            return zf.read("content.xml").decode("utf-8")
    finally:
        if os.path.exists(path):
            os.remove(path)


@native_test
@with_native_doc("writer")
def test_section_create_keeps_paragraphs_uno(ctx, doc):
    _fill_paragraphs(doc, count=6)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    orig_text = doc.getText().getString()

    paras = _paragraph_elements(doc)
    sel_cur = doc.getText().createTextCursorByRange(paras[1].getStart())
    sel_cur.goRight(5, False)
    end_cur = doc.getText().createTextCursorByRange(paras[2].getStart())
    end_cur.goRight(8, False)
    sel_cur.gotoRange(end_cur, True)
    doc.getCurrentController().select(sel_cur)

    res = SectionCreate().execute(tctx, name="Cols", locator="selection")
    assert res["status"] == "ok"
    assert res["section_name"] == "Cols"

    paras_after = _paragraph_elements(doc)
    assert len(paras_after) == 6
    assert doc.getText().getString() == orig_text
    assert paras_after[0].getPropertyValue("TextSection") is None
    assert paras_after[1].getPropertyValue("TextSection").getName() == "Cols"
    assert paras_after[2].getPropertyValue("TextSection").getName() == "Cols"
    assert paras_after[3].getPropertyValue("TextSection") is None

    res_tail = SectionCreate().execute(
        tctx,
        name="Tail",
        locator="paragraph:4",
        end_locator="paragraph:5",
    )
    assert res_tail["status"] == "ok"
    paras_tail = _paragraph_elements(doc)
    assert paras_tail[4].getPropertyValue("TextSection").getName() == "Tail"
    assert paras_tail[5].getPropertyValue("TextSection").getName() == "Tail"

    dup = SectionCreate().execute(tctx, name="Cols", locator="paragraph:0")
    assert dup["status"] == "error"
    assert doc.getTextSections().getCount() == 2

    overlap = SectionCreate().execute(
        tctx,
        name="Overlap",
        locator="paragraph:2",
        end_locator="paragraph:3",
    )
    assert overlap["status"] == "error"
    assert overlap["code"] == "CREATE_SECTION_FAILED"
    assert doc.getTextSections().getCount() == 2
    assert doc.getText().getString() == orig_text


@native_test
@with_native_doc("writer")
def test_section_columns_and_protection_saved_uno(ctx, doc):
    _fill_paragraphs(doc, count=4)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    assert SectionCreate().execute(tctx, name="Cols", locator="paragraph:0", end_locator="paragraph:1")["status"] == "ok"
    assert SectionSetColumns().execute(tctx, name="Cols", column_count=2, spacing_mm=5)["status"] == "ok"
    assert SectionSetProtection().execute(tctx, name="Cols", protected=True)["status"] == "ok"

    xml = _save_odt_content_xml(doc)
    assert 'text:name="Cols"' in xml
    assert 'text:protected="true"' in xml
    assert 'fo:column-count="2"' in xml

    sec = doc.getTextSections().getByName("Cols")
    assert sec.getPropertyValue("IsProtected") is True
    doc.getUndoManager().undo()
    assert sec.getPropertyValue("IsProtected") is False


@native_test
@with_native_doc("writer")
def test_section_delete_keeps_text_uno(ctx, doc):
    _fill_paragraphs(doc, count=5)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    orig_text = doc.getText().getString()

    assert SectionCreate().execute(tctx, name="Outer", locator="paragraph:1", end_locator="paragraph:3")["status"] == "ok"
    assert SectionCreate().execute(tctx, name="Inner", locator="paragraph:2")["status"] == "ok"

    res = SectionDelete().execute(tctx, name="Outer")
    assert res["status"] == "ok"
    assert res["deleted"] is True

    assert doc.getText().getString() == orig_text
    assert len(_paragraph_elements(doc)) == 5
    assert not doc.getTextSections().hasByName("Outer")
    assert doc.getTextSections().hasByName("Inner")

    xml = _save_odt_content_xml(doc)
    assert xml.count("<text:section") == 1


@native_test
@with_native_doc("writer")
def test_section_guards_uno(ctx, doc):
    _fill_paragraphs(doc, count=4)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    assert SectionCreate().execute(tctx, name="Locked", locator="paragraph:0")["status"] == "ok"
    locked = doc.getTextSections().getByName("Locked")
    locked.setPropertyValue("ProtectionKey", uno.ByteSequence(b"\x01" * 20))
    locked.setPropertyValue("IsProtected", True)

    prot_res = SectionSetProtection().execute(tctx, name="Locked", protected=False)
    assert prot_res["status"] == "error"
    assert prot_res["code"] == "SECTION_PASSWORD_PROTECTED"
    assert locked.getPropertyValue("IsProtected") is True

    del_res = SectionDelete().execute(tctx, name="Locked")
    assert del_res["status"] == "error"
    assert del_res["code"] == "SECTION_PASSWORD_PROTECTED"
    assert doc.getTextSections().hasByName("Locked")

    toc = doc.createInstance("com.sun.star.text.ContentIndex")
    end_cur = doc.getText().createTextCursor()
    end_cur.gotoEnd(False)
    doc.getText().insertTextContent(end_cur, toc, False)
    toc.update()

    toc_del = SectionDelete().execute(tctx, name="Table of Contents1")
    assert toc_del["status"] == "error"
    assert toc_del["code"] == "INDEX_SECTION"
    assert doc.getDocumentIndexes().getCount() == 1
    assert doc.getTextSections().hasByName("Table of Contents1")
