# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock
import pytest
from plugin.framework.errors import DocumentDisposedError
from plugin.writer.specialized.shapes import replace_text_in_shape


class DisposedException(Exception):
    pass


def test_replace_text_in_shape_reraises_disposal():
    shape = MagicMock()
    shape.getText.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        replace_text_in_shape(shape, "a", "b")


def test_replace_text_in_shape_returns_false_on_ordinary_error():
    shape = MagicMock()
    shape.getText.side_effect = RuntimeError("boom")
    assert replace_text_in_shape(shape, "a", "b") is False


def test_replace_text_in_shape_folds_quotes():
    xtext = MagicMock()
    xtext.getString.return_value = "He said \u201cHi,\u201d"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, '"Hi,"', "Bye") is True
    cursor.goRight.assert_any_call(8, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("Bye")


def test_replace_text_in_shape_case_sensitivity():
    xtext = MagicMock()
    xtext.getString.return_value = "Hello World"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "world", "Universe", case_sensitive=False) is True
    cursor.goRight.assert_any_call(6, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("Universe")

    assert replace_text_in_shape(shape, "world", "Universe", case_sensitive=True) is False


def test_replace_text_in_shape_regex():
    xtext = MagicMock()
    xtext.getString.return_value = "say hello!"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "h.+o", "goodbye", use_regex=True) is True
    cursor.goRight.assert_any_call(4, False)
    cursor.goRight.assert_any_call(5, True)
    cursor.setString.assert_called_once_with("goodbye")


def test_replace_text_in_shape_regex_empty_match_returns_false():
    xtext = MagicMock()
    xtext.getString.return_value = "hello"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    for pat in ("x*", "^"):
        assert replace_text_in_shape(shape, pat, "bar", use_regex=True) is False
    cursor.setString.assert_not_called()
    cursor.goRight.assert_not_called()


def test_replace_text_in_shape_regex_skips_leading_empty():
    xtext = MagicMock()
    xtext.getString.return_value = "ab"
    cursor = MagicMock()
    xtext.createTextCursorByRange.return_value = cursor
    shape = MagicMock()
    shape.getText.return_value = xtext

    assert replace_text_in_shape(shape, "b*", "B", use_regex=True) is True
    cursor.goRight.assert_any_call(1, False)
    cursor.goRight.assert_any_call(1, True)
    cursor.setString.assert_called_once_with("B")


def test_writer_layout_tool_names():
    from plugin.writer.specialized import shapes as writer_shapes

    assert writer_shapes.AlignShapes.name == "shape_align"
    assert writer_shapes.DistributeShapes.name == "shape_distribute"
    assert writer_shapes.CreateDiagram.name == "shape_diagram"
