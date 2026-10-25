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
