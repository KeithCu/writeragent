# WriterAgent - Native UNO Test for Fields
import uno  # noqa: F401

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.fields import FieldsDelete, FieldsInsert


def _set_body(doc, text_value):
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


def _count_text_fields(doc):
    enum = doc.getTextFields().createEnumeration()
    count = 0
    while enum.hasMoreElements():
        field = enum.nextElement()
        if not field.supportsService("com.sun.star.text.textfield.Annotation"):
            count += 1
    return count


@native_test
@with_native_doc("writer")
def test_fields_delete_body_and_footer(ctx, doc):
    """Field delete in body and footer: words stay, field count drops, no tracked deletions on neighbors."""
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    # 1. Body field between two words
    _set_body(doc, "alpha omega")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.goRight(6, False)  # after "alpha "
    field1 = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    doc.getText().insertTextContent(cur, field1, False)

    # 2. Footer field between two words
    style_families = doc.getStyleFamilies()
    page_styles = style_families.getByName("PageStyles")
    style = page_styles.getByName("Standard")
    style.setPropertyValue("FooterIsOn", True)
    footer = style.getPropertyValue("FooterText")
    footer.setString("left right")
    f_cur = footer.createTextCursor()
    f_cur.gotoStart(False)
    f_cur.goRight(5, False)  # after "left "
    field2 = doc.createInstance("com.sun.star.text.textfield.PageNumber")
    footer.insertTextContent(f_cur, field2, False)

    count_before = _count_text_fields(doc)
    assert count_before == 2

    # Delete body field (ID 1)
    res1 = FieldsDelete().execute(tool_ctx, ids=[1])
    assert res1["status"] == "ok"
    assert res1["deleted_count"] == 1
    assert _count_text_fields(doc) == count_before - 1
    assert "alpha omega" in doc.getText().getString()

    # Repeat with RecordChanges on for footer field
    doc.setPropertyValue("RecordChanges", True)
    try:
        res2 = FieldsDelete().execute(tool_ctx, ids=[1])  # remaining footer field is ID 1
        assert res2["status"] == "ok"
        assert res2["deleted_count"] == 1
        # Assert footer text still has neighboring words and they are not tracked deletions
        assert "left right" in footer.getString()
        footer_clean = get_string_without_tracked_deletions(footer)
        assert "left" in footer_clean and "right" in footer_clean
        body_clean = get_string_without_tracked_deletions(doc.getText())
        assert "alpha" in body_clean and "omega" in body_clean
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_fields_insert_search_absorb(ctx, doc):
    """Search insert replaces TARGET with field; neighbors stay and field count increases."""
    _set_body(doc, "alpha TARGET omega")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    count_before = _count_text_fields(doc)
    res = FieldsInsert().execute(tool_ctx, field="PageNumber", target="search", old_content="TARGET")
    assert res["status"] == "ok", res
    assert _count_text_fields(doc) == count_before + 1

    body = doc.getText().getString()
    assert "TARGET" not in body
    assert "alpha" in body
    assert "omega" in body


@native_test
@with_native_doc("writer")
def test_fields_insert_page_number_visible_default(ctx, doc):
    """PageNumber inserted with no properties defaults to NumberingType=4 and SubType=CURRENT, visible on page 1."""
    from com.sun.star.text.PageNumberType import CURRENT

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = FieldsInsert().execute(tool_ctx, field="PageNumber", target="end")
    assert res["status"] == "ok", res

    enum = doc.getTextFields().createEnumeration()
    page_field = None
    while enum.hasMoreElements():
        f = enum.nextElement()
        if f.supportsService("com.sun.star.text.textfield.PageNumber"):
            page_field = f
            break

    assert page_field is not None, "PageNumber field was not inserted"
    assert page_field.getPropertyValue("NumberingType") == 4
    assert page_field.getPropertyValue("SubType") == CURRENT
    # Must be visible (renders '1' on page 1), not blank
    assert page_field.getPresentation(False) == "1"

