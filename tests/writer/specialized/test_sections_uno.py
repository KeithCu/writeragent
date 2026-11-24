import base64
import hashlib
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import uno

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.sections import (
    SectionCreate,
    SectionDelete,
    SectionGetInfo,
    SectionRename,
    SectionSetColumns,
    SectionSetMargins,
    SectionSetPassword,
    SectionSetProtection,
    SectionSetVisibility,
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
    root = ET.fromstring(xml)
    text_ns = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    style_ns = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
    fo_ns = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
    cols_elem = next(
        el
        for el in root.iter(f"{{{text_ns}}}section")
        if el.attrib.get(f"{{{text_ns}}}name") == "Cols"
    )
    assert cols_elem.attrib.get(f"{{{text_ns}}}protected") == "true"
    style_name = cols_elem.attrib[f"{{{text_ns}}}style-name"]
    style_elem = next(
        el
        for el in root.iter(f"{{{style_ns}}}style")
        if el.attrib.get(f"{{{style_ns}}}name") == style_name
    )
    columns_elem = style_elem.find(f".//{{{style_ns}}}columns")
    assert columns_elem is not None
    assert columns_elem.attrib.get(f"{{{fo_ns}}}column-count") == "2"

    sec = doc.getTextSections().getByName("Cols")
    assert sec.getPropertyValue("TextColumns").getColumnCount() == 2
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

    toc_create = SectionCreate().execute(tctx, name="InsideTOC", locator="paragraph:4")
    assert toc_create["status"] == "error"
    assert toc_create["code"] == "INDEX_SECTION"
    assert not doc.getTextSections().hasByName("InsideTOC")


@native_test
@with_native_doc("writer")
def test_section_visibility_changes_layout_uno(ctx, doc):
    _fill_paragraphs(doc, count=160)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    assert SectionCreate().execute(tctx, name="Big", locator="paragraph:2", end_locator="paragraph:150")["status"] == "ok"

    controller = doc.getCurrentController()
    start_pages = controller.PageCount
    assert start_pages > 1

    hide_res = SectionSetVisibility().execute(tctx, name="Big", visible=False)
    assert hide_res["status"] == "ok"
    assert hide_res["is_visible"] is False
    assert hide_res["is_shown"] is False
    assert controller.PageCount < start_pages

    doc.getUndoManager().undo()
    assert controller.PageCount == start_pages

    cond_show = SectionSetVisibility().execute(tctx, name="Big", visible=False, condition="1==0")
    assert cond_show["status"] == "ok"
    assert cond_show["is_shown"] is True
    assert controller.PageCount == start_pages

    cond_hide = SectionSetVisibility().execute(tctx, name="Big", visible=False, condition="1==1")
    assert cond_hide["status"] == "ok"
    assert cond_hide["is_shown"] is False
    assert controller.PageCount < start_pages

    info = SectionGetInfo().execute(tctx, name="Big")
    assert info["status"] == "ok"
    assert info["condition"] == "1==1"
    assert info["is_visible"] is False

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    prop.Name = "Hidden"
    prop.Value = True
    doc2 = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (prop,))
    try:
        _fill_paragraphs(doc2, count=3)
        tctx2 = TestingFactory.create_context(doc=doc2, ctx=ctx, env="native")
        assert SectionCreate().execute(tctx2, name="All", locator="paragraph:0", end_locator="paragraph:2")["status"] == "ok"
        fail_res = SectionSetVisibility().execute(tctx2, name="All", visible=False)
        assert fail_res["status"] == "error"
        assert fail_res["code"] == "SET_SECTION_VISIBILITY_FAILED"
        assert doc2.getTextSections().getByName("All").getPropertyValue("IsVisible") is True
    finally:
        doc2.close(True)


@native_test
@with_native_doc("writer")
def test_section_password_round_trip_uno(ctx, doc):
    _fill_paragraphs(doc, count=4)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    assert SectionCreate().execute(tctx, name="Secret", locator="paragraph:0", end_locator="paragraph:1")["status"] == "ok"

    pw = "Gehe1m-ü"
    set_res = SectionSetPassword().execute(tctx, name="Secret", password=pw)
    assert set_res["status"] == "ok"
    assert set_res["has_password"] is True
    assert set_res["is_protected"] is True

    sec = doc.getTextSections().getByName("Secret")
    assert sec.getPropertyValue("IsProtected") is True

    expected_b64 = base64.b64encode(hashlib.sha1(pw.encode("utf-16-le"), usedforsecurity=False).digest()).decode("ascii")
    xml = _save_odt_content_xml(doc)
    root = ET.fromstring(xml)
    text_ns = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    sec_elem = next(el for el in root.iter(f"{{{text_ns}}}section") if el.attrib.get(f"{{{text_ns}}}name") == "Secret")
    assert sec_elem.attrib.get(f"{{{text_ns}}}protected") == "true"
    assert sec_elem.attrib.get(f"{{{text_ns}}}protection-key") == expected_b64

    prot_res = SectionSetProtection().execute(tctx, name="Secret", protected=False)
    assert prot_res["status"] == "error"
    assert prot_res["code"] == "SECTION_PASSWORD_PROTECTED"

    bad_pw = SectionSetPassword().execute(tctx, name="Secret", password="", current_password="nope")
    assert bad_pw["status"] == "error"
    assert bad_pw["code"] == "INVALID_PARAM"

    locked_info = SectionGetInfo().execute(tctx, name="Secret")
    assert locked_info["status"] == "ok"
    assert locked_info["has_password"] is True

    clear_res = SectionSetPassword().execute(tctx, name="Secret", password="", current_password=pw)
    assert clear_res["status"] == "ok"
    assert clear_res["has_password"] is False
    assert clear_res["is_protected"] is True

    xml_after = _save_odt_content_xml(doc)
    root_after = ET.fromstring(xml_after)
    sec_elem_after = next(
        el for el in root_after.iter(f"{{{text_ns}}}section") if el.attrib.get(f"{{{text_ns}}}name") == "Secret"
    )
    assert f"{{{text_ns}}}protection-key" not in sec_elem_after.attrib


@native_test
@with_native_doc("writer")
def test_section_rename_and_margins_uno(ctx, doc):
    _fill_paragraphs(doc, count=5)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    assert SectionCreate().execute(tctx, name="M", locator="paragraph:1", end_locator="paragraph:2")["status"] == "ok"
    assert SectionCreate().execute(tctx, name="Other", locator="paragraph:4")["status"] == "ok"

    ren_res = SectionRename().execute(tctx, name="M", new_name="Renamed")
    assert ren_res["status"] == "ok"
    assert ren_res["section_name"] == "Renamed"
    assert ren_res["old_name"] == "M"
    assert not doc.getTextSections().hasByName("M")
    assert doc.getTextSections().hasByName("Renamed")

    dup_res = SectionRename().execute(tctx, name="Renamed", new_name="Other")
    assert dup_res["status"] == "error"
    assert dup_res["code"] == "INVALID_PARAM"
    assert doc.getTextSections().hasByName("Renamed")

    doc.getUndoManager().undo()
    assert doc.getTextSections().hasByName("M")
    assert not doc.getTextSections().hasByName("Renamed")

    margin_res = SectionSetMargins().execute(tctx, name="M", left_mm=15, right_mm=5)
    assert margin_res["status"] == "ok"
    info = SectionGetInfo().execute(tctx, name="M")
    assert info["status"] == "ok"
    assert abs(info["left_margin_mm"] - 15) <= 0.02
    assert abs(info["right_margin_mm"] - 5) <= 0.02

    paras = _paragraph_elements(doc)
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(paras[0].getStart(), False)
    outside_x = vc.getPosition().X
    vc.gotoRange(paras[1].getStart(), False)
    inside_x = vc.getPosition().X
    expected_hmm = round(info["left_margin_mm"] * 100)
    assert abs((inside_x - outside_x) - expected_hmm) <= 2

    right_only = SectionSetMargins().execute(tctx, name="M", right_mm=7)
    assert right_only["status"] == "ok"
    assert abs(right_only["left_margin_mm"] - info["left_margin_mm"]) <= 0.02
    assert abs(right_only["right_margin_mm"] - 7) <= 0.02

    doc.getUndoManager().undo()
    restored = SectionGetInfo().execute(tctx, name="M")
    assert restored["left_margin_mm"] == info["left_margin_mm"]
    assert restored["right_margin_mm"] == info["right_margin_mm"]


@native_test
@with_native_doc("writer")
def test_section_get_info_nesting_and_index_uno(ctx, doc):
    _fill_paragraphs(doc, count=6)
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    assert SectionCreate().execute(tctx, name="Outer", locator="paragraph:1", end_locator="paragraph:4")["status"] == "ok"
    assert SectionCreate().execute(tctx, name="Inner", locator="paragraph:2", end_locator="paragraph:3")["status"] == "ok"

    outer_info = SectionGetInfo().execute(tctx, name="Outer")
    assert outer_info["status"] == "ok"
    assert outer_info["parent_section"] is None
    assert outer_info["child_sections"] == ["Inner"]

    inner_info = SectionGetInfo().execute(tctx, name="Inner")
    assert inner_info["status"] == "ok"
    assert inner_info["parent_section"] == "Outer"
    assert inner_info["child_sections"] == []
    assert inner_info["is_shown"] is True

    assert SectionSetVisibility().execute(tctx, name="Outer", visible=False)["status"] == "ok"
    inner_hidden = SectionGetInfo().execute(tctx, name="Inner")
    assert inner_hidden["status"] == "ok"
    assert inner_hidden["is_visible"] is True
    assert inner_hidden["is_shown"] is False

    toc = doc.createInstance("com.sun.star.text.ContentIndex")
    end_cur = doc.getText().createTextCursor()
    end_cur.gotoEnd(False)
    doc.getText().insertTextContent(end_cur, toc, False)
    toc.update()

    toc_info = SectionGetInfo().execute(tctx, name="Table of Contents1")
    assert toc_info["status"] == "ok"
    assert toc_info["is_index"] is True


@native_test
@with_native_doc("writer")
def test_section_read_hides_multi_para_tracked_deletion_uno(ctx, doc):
    from plugin.tests.testing_utils import strike_across_paragraphs
    from plugin.writer.specialized.sections import SectionRead

    text = doc.getText()
    strike_across_paragraphs(doc, text)

    sec_cur = text.createTextCursor()
    sec_cur.gotoStart(False)
    sec_cur.gotoEnd(True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("StruckSec")
    text.insertTextContent(sec_cur, sec, True)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    res = SectionRead().execute(tctx, name="StruckSec")
    assert res["status"] == "ok", res
    assert "gone" not in res["content"], res
    assert "Keep" in res["content"] and "Tail" in res["content"] and "After" in res["content"]


@native_test
@with_native_doc("writer")
def test_section_starting_mid_deletion_hides_deleted_text_uno(ctx, doc):
    from plugin.tests.testing_utils import strike_across_paragraphs
    from plugin.writer.specialized.sections import SectionRead

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    enum = text.createEnumeration()
    paras = []
    while enum.hasMoreElements():
        paras.append(enum.nextElement())

    # Wrap only middle + end + after paragraphs so the section starts mid-deletion.
    sec_cur = text.createTextCursorByRange(paras[2].getStart())
    sec_cur.gotoRange(paras[4].getEnd(), True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("MidSec")
    text.insertTextContent(sec_cur, sec, True)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    res = SectionRead().execute(tctx, name="MidSec")
    assert res["status"] == "ok", res
    assert "gone" not in res["content"], res
    assert "Tail" in res["content"] and "After" in res["content"], res
