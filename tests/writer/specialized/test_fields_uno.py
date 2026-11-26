# WriterAgent - Native UNO Test for Fields
import uno  # noqa: F401

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.framework.uno_context import enum_has_more
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
    while enum_has_more(enum):
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
    assert "tracked" not in res
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
    while enum_has_more(enum):
        f = enum.nextElement()
        if f.supportsService("com.sun.star.text.textfield.PageNumber"):
            page_field = f
            break

    assert page_field is not None, "PageNumber field was not inserted"
    assert page_field.getPropertyValue("NumberingType") == 4
    assert page_field.getPropertyValue("SubType") == CURRENT
    # Must be visible (renders '1' on page 1), not blank
    assert page_field.getPresentation(False) == "1"


@native_test
@with_native_doc("writer")
def test_fields_insert_recorded_accept_reject_and_token_uno(ctx, doc):
    """PageCount, Input, and merge fields record cleanly in RecordChanges and Record mode."""
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.specialized.mail_merge import InsertField

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    # 1. User recording: PageCount and Input Accept/Reject cleanly
    for field_name, props in (("PageCount", {}), ("Input", {"Content": "prompt"})):
        _set_body(doc, "hello MATCH world")
        doc.setPropertyValue("RecordChanges", True)
        try:
            res = FieldsInsert().execute(
                tool_ctx, field=field_name, properties=props, target="search", old_content="MATCH"
            )
            assert res["status"] == "ok", res
            assert res["tracked"] is True and res["pending_review"] is True, res
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
            assert doc.getText().getString() == "hello MATCH world"
            assert _count_text_fields(doc) == 0

            res2 = FieldsInsert().execute(
                tool_ctx, field=field_name, properties=props, target="search", old_content="MATCH"
            )
            assert res2["status"] == "ok", res2
            assert res2["tracked"] is True and res2["pending_review"] is True, res2
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            body_accept = doc.getText().getString()
            assert "MATCH" not in body_accept
            assert "hello " in body_accept and " world" in body_accept
            assert _count_text_fields(doc) == 1
        finally:
            doc.setPropertyValue("RecordChanges", False)

    # 2. Record mode tags agent token for PageCount, Input, and merge field
    with (
        __import__("unittest.mock").mock.patch(
            "plugin.writer.edit_review.review_recording_enabled", return_value=True
        ),
        __import__("unittest.mock").mock.patch(
            "plugin.writer.edit_review.edit_review_wait_seconds", return_value=0
        ),
    ):
        for field_name, props in (("PageCount", {}), ("Input", {"Content": "prompt"})):
            _set_body(doc, "hello MATCH world")
            res_f = FieldsInsert().execute(
                tool_ctx, field=field_name, properties=props, target="search", old_content="MATCH"
            )
            assert res_f["status"] == "ok", res_f
            assert res_f["tracked"] is True and res_f["pending_review"] is True, res_f
            redlines = doc.getRedlines()
            comments = [
                str(redlines.getByIndex(i).getPropertyValue("RedlineComment") or "")
                for i in range(redlines.getCount())
            ]
            assert any(c.startswith("wa-review:") for c in comments), (field_name, comments)
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
            assert doc.getText().getString() == "hello MATCH world"

        _set_body(doc, "dear NAME,")
        res_m = InsertField().execute(
            tool_ctx, column_name="First", data_source_name="Bib", table_name="biblio",
            target="search", old_content="NAME",
        )
        assert res_m["status"] == "ok", res_m
        assert res_m["tracked"] is True and res_m["pending_review"] is True, res_m
        redlines = doc.getRedlines()
        assert redlines.getCount() >= 1
        comments = [str(redlines.getByIndex(i).getPropertyValue("RedlineComment") or "") for i in range(redlines.getCount())]
        assert any(c.startswith("wa-review:") for c in comments), comments
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getText().getString() == "dear NAME,"


@native_test
@with_native_doc("writer")
def test_fields_insert_refuses_crossing_deletion_uno(ctx, doc):
    from plugin.doc.text_helpers import visible_range_text
    from tests.writer.doc_builders import reject_all_changes

    _set_body(doc, "Word OLD here")
    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.goRight(len("Word "), False)
    cur.goRight(len("OLD "), True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = FieldsInsert().execute(tool_ctx, field="PageCount", target="search", old_content="Word here")
    assert res["status"] == "error" and res["code"] == "MATCH_CROSSES_DELETION", res
    assert visible_range_text(doc, doc.getText()) == "Word here"
    assert doc.getRedlines().getCount() == 1
    assert _count_text_fields(doc) == 0

    doc.setPropertyValue("RecordChanges", True)
    try:
        ok = FieldsInsert().execute(tool_ctx, field="PageCount", target="search", old_content="Word here")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert ok["status"] == "ok", ok
    assert _count_text_fields(doc) == 1
    reject_all_changes(doc, ctx)
    assert doc.getText().getString() == "Word OLD here"

