# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for get_string_without_tracked_deletions paragraph vs multi-para walks."""

from plugin.doc.text_helpers import _visible_portions, get_string_without_tracked_deletions
from plugin.framework.uno_context import enum_has_more, get_desktop
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory
from plugin.writer.html_export import _visible_portions as html_visible_portions


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0
_BOLD = 150.0


def _first_paragraph(doc):
    return doc.getText().createEnumeration().nextElement()


@native_test
def test_has_more_elements_returns_python_bool(ctx):
    """PyUNO maps sal_Bool from hasMoreElements() to bool, not integer 1."""

    def _assert_bool_row(enum, expected):
        value = enum.hasMoreElements()
        assert value is expected
        assert type(value) is bool
        assert enum_has_more(enum) is expected

    with TestingFactory.native_doc(ctx, "writer") as doc:
        enum = doc.getText().createEnumeration()
        _assert_bool_row(enum, True)
        enum.nextElement()
        _assert_bool_row(enum, False)

    desktop = get_desktop(ctx)
    enum = desktop.getComponents().createEnumeration()
    _assert_bool_row(enum, True)
    while enum_has_more(enum):
        enum.nextElement()
    _assert_bool_row(enum, False)

    with TestingFactory.native_doc(ctx, "calc") as doc:
        enum = doc.getSheets().createEnumeration()
        _assert_bool_row(enum, True)
        while enum_has_more(enum):
            enum.nextElement()
        _assert_bool_row(enum, False)

    with TestingFactory.native_doc(ctx, "draw") as doc:
        page = doc.getDrawPages().getByIndex(0)
        enum = page.createAnnotationEnumeration()
        assert enum is not None
        _assert_bool_row(enum, False)
        page.createAndInsertAnnotation()
        fresh_enum = page.createAnnotationEnumeration()
        _assert_bool_row(fresh_enum, True)
        fresh_enum.nextElement()
        _assert_bool_row(fresh_enum, False)


@native_test
def test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline(ctx):
    """A paragraph with a bold run must stay one line (portions are not paragraphs)."""
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Paragraph with normal and bold text", False)
        cursor.gotoStart(False)
        cursor.goRight(len("Paragraph with "), False)
        cursor.goRight(len("normal and bold"), True)
        cursor.setPropertyValue("CharWeight", _BOLD)

        para = _first_paragraph(doc)
        assert para.supportsService("com.sun.star.text.Paragraph")
        visible = para.getString()
        got = get_string_without_tracked_deletions(para)
        assert got == visible
        assert "\n" not in got
        assert got == "".join(chunk for _unused, chunk in _visible_portions(para))
        assert got == "".join(chunk for _unused, chunk in html_visible_portions(para))

        # A cursor covering that single paragraph is document-mode (one child)
        # and must still match the visible string.
        full = text.createTextCursor()
        full.gotoStart(False)
        full.gotoEnd(True)
        assert get_string_without_tracked_deletions(full) == visible


@native_test
def test_get_string_without_tracked_deletions_multi_para_joins_with_newline(ctx):
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "First para", False)
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        text.insertString(cursor, "Second para", False)

        expected = "First para\nSecond para"
        assert get_string_without_tracked_deletions(text) == expected

        full = text.createTextCursor()
        full.gotoStart(False)
        full.gotoEnd(True)
        assert get_string_without_tracked_deletions(full) == expected


def _rect(doc, text=""):
    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    doc.getDrawPage().add(shape)
    if text:
        shape.setString(text)
    return shape


@native_test
def test_get_string_without_tracked_deletions_draw_shapes_read_text_uno(ctx):
    """Draw shapes return their text directly via getString."""
    with TestingFactory.native_doc(ctx, "writer") as doc:
        shapes = [_rect(doc) for _idx in range(40)]
        last_shape = _rect(doc, "alpha\nbeta")
        shapes.append(last_shape)

        for _round in range(3):
            for i, s in enumerate(shapes):
                got = get_string_without_tracked_deletions(s)
                if i < 40:
                    assert got == ""
                else:
                    assert got == "alpha\nbeta"
                    assert got == s.getString()
