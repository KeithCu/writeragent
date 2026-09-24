# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native tests: Calc and Writer document pooling (default) and reuse=False fresh docs."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

_calc_uids: list = []
_writer_uids: list = []


def _runtime_uid(doc) -> str:
    try:
        return str(doc.RuntimeUID)
    except Exception:
        return str(doc.getURL())


@native_test
@with_native_doc("calc")
def test_calc_reuse_writes_then_leaves_dirt(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setString("leftover-calc")
    from plugin.scripting.document_scripts import set_calc_init_script

    set_calc_init_script(doc, "def double(x):\n    return x * 2")
    _calc_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("calc")
def test_calc_reuse_next_test_sees_empty_sheet(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    assert sheet.getCellByPosition(0, 0).getString() == ""
    if _calc_uids:
        assert _runtime_uid(doc) == _calc_uids[0]
    from plugin.scripting.document_scripts import get_calc_init_script

    assert (get_calc_init_script(doc) or "").strip() == ""
    assert sheet.getCharts().getCount() == 0


@native_test
@with_native_doc("writer", reuse=True)
def test_writer_reuse_writes_then_leaves_dirt(ctx, doc):
    from plugin.writer.format import insert_content_at_position

    insert_content_at_position(
        doc,
        ctx,
        '<h2 style="font-size: 14pt; font-weight: bold;">SECTION HEADING</h2>'
        '<p>Body paragraph text.</p>',
        "end",
    )
    _writer_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("writer", reuse=True)
def test_writer_reuse_next_test_sees_empty_text(ctx, doc):
    assert doc.getText().getString() == ""
    if _writer_uids:
        assert _runtime_uid(doc) == _writer_uids[0]
    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    weight = float(cursor.getPropertyValue("CharWeight"))
    assert weight < 135.0, f"empty para leaked bold CharWeight={weight!r}"
    para = str(cursor.getPropertyValue("ParaStyleName") or "")
    assert para in ("", "Standard"), f"empty para leaked style {para!r}"


@native_test
@with_native_doc("writer")
def test_writer_default_reuse_writes_then_leaves_dirt(ctx, doc):
    from plugin.writer.format import insert_content_at_position

    insert_content_at_position(
        doc,
        ctx,
        '<h2 style="font-size: 14pt; font-weight: bold;">SECTION HEADING</h2>'
        '<p>Body paragraph text.</p>',
        "end",
    )
    _writer_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("writer")
def test_writer_default_reuse_next_test_sees_empty_text(ctx, doc):
    assert doc.getText().getString() == ""
    if _writer_uids:
        assert _runtime_uid(doc) == _writer_uids[-1]
    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    weight = float(cursor.getPropertyValue("CharWeight"))
    assert weight < 135.0, f"empty para leaked bold CharWeight={weight!r}"
    para = str(cursor.getPropertyValue("ParaStyleName") or "")
    assert para in ("", "Standard"), f"empty para leaked style {para!r}"


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_reuse_false_still_empty(ctx, doc):
    assert doc.getSheets().getByIndex(0).getCellByPosition(0, 0).getString() == ""


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_reuse_false_still_empty(ctx, doc):
    assert doc.getText().getString() == ""
