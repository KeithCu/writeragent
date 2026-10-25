"""Unit tests for plugin/writer/range_copy.py."""
from unittest.mock import MagicMock, patch

from plugin.writer import range_copy as rc


class _Enum:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def hasMoreElements(self) -> bool:
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


def test_copy_cell_xtext_copies_direct_char_formatting() -> None:
    portion = MagicMock()
    portion.getPropertyValue.return_value = "Text"
    portion.getString.return_value = "Bold"

    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])

    src_cell = MagicMock()
    src_cell.createEnumeration.return_value = _Enum([para])

    dest_doc = MagicMock()
    dest_cell = MagicMock()

    with patch.object(rc, "_copy_properties") as copy_props, \
            patch.object(rc, "_copy_math_or_text") as math_or_text:
        rc._copy_cell_xtext(src_cell, dest_doc, dest_cell)

    assert [call.args[2] for call in copy_props.call_args_list] == [rc._COPIED_CHAR_PROPERTIES]
    math_or_text.assert_not_called()
