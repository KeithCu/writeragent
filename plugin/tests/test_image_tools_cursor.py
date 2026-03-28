import sys
import types
import unittest
from unittest.mock import MagicMock


def _install_uno_mocks():
    """
    image_tools.py imports several LibreOffice UNO types at module import time.
    The unit tests in this repo sometimes run without LibreOffice present, so we
    stub the minimal module structure needed for imports.
    """
    uno_mock = MagicMock()
    uno_mock.systemPathToFileUrl.side_effect = lambda p: f"file:///{p}"
    sys.modules["uno"] = uno_mock

    # Build `com.sun.star.*` module structure.
    sys.modules["com"] = types.ModuleType("com")
    sys.modules["com.sun"] = types.ModuleType("com.sun")
    sys.modules["com.sun.star"] = types.ModuleType("com.sun.star")

    sys.modules["com.sun.star.text"] = types.ModuleType("com.sun.star.text")
    anchor_mod = types.ModuleType("com.sun.star.text.TextContentAnchorType")
    setattr(anchor_mod, "AS_CHARACTER", 1)
    setattr(anchor_mod, "AT_FRAME", 3)
    sys.modules["com.sun.star.text.TextContentAnchorType"] = anchor_mod

    awt_mod = types.ModuleType("com.sun.star.awt")

    class Size:
        def __init__(self):
            self.Width = 0
            self.Height = 0

    class Point:
        def __init__(self, x, y):
            self.X = x
            self.Y = y

    setattr(awt_mod, "Size", Size)
    setattr(awt_mod, "Point", Point)
    sys.modules["com.sun.star.awt"] = awt_mod

    beans_mod = types.ModuleType("com.sun.star.beans")

    class PropertyValue:
        def __init__(self, Name=None, Value=None):
            self.Name = Name
            self.Value = Value

    setattr(beans_mod, "PropertyValue", PropertyValue)
    sys.modules["com.sun.star.beans"] = beans_mod


_install_uno_mocks()

from plugin.framework import image_tools  # noqa: E402


class TestWriterImageCursorConversion(unittest.TestCase):
    def test_insert_image_to_writer_uses_text_cursor(self):
        doc_text = MagicMock()
        text_cursor = MagicMock(name="text_cursor")
        doc_text.createTextCursorByRange.return_value = text_cursor
        doc_text.insertTextContent = MagicMock()

        view_cursor = MagicMock(name="view_cursor")
        view_cursor.getStart.return_value = "range-start"
        view_cursor.jumpToStartOfPage = MagicMock()

        model = MagicMock()
        model.getText.return_value = doc_text
        model.CurrentController = MagicMock()
        model.CurrentController.ViewCursor = view_cursor
        image_instance = MagicMock(name="image_instance")
        model.createInstance.return_value = image_instance
        model.Text = MagicMock()  # should not be used by the fixed code

        image_tools._insert_image_to_writer(
            model,
            "/tmp/img.png",
            width=10,
            height=20,
            title="t",
            description="d",
            add_frame=False,
        )

        doc_text.createTextCursorByRange.assert_called_once_with("range-start")
        doc_text.insertTextContent.assert_called_once_with(text_cursor, image_instance, False)

    def test_insert_image_to_writer_fallback_rejumps_and_recreates_cursor(self):
        doc_text = MagicMock()
        doc_text.insertTextContent = MagicMock()
        doc_text.insertTextContent.side_effect = [RuntimeError("no selection"), None]

        view_cursor = MagicMock(name="view_cursor")
        view_cursor.getStart.side_effect = ["range1", "range2"]
        view_cursor.jumpToStartOfPage = MagicMock()

        model = MagicMock()
        model.getText.return_value = doc_text
        model.CurrentController = MagicMock()
        model.CurrentController.ViewCursor = view_cursor
        image_instance = MagicMock(name="image_instance")
        model.createInstance.return_value = image_instance
        model.Text = MagicMock()

        # Different cursor objects for first attempt vs fallback attempt.
        doc_text.createTextCursorByRange.side_effect = ["tc1", "tc2"]

        image_tools._insert_image_to_writer(
            model,
            "/tmp/img.png",
            width=10,
            height=20,
            title="t",
            description="d",
            add_frame=False,
        )

        view_cursor.jumpToStartOfPage.assert_called_once()
        self.assertEqual(doc_text.insertTextContent.call_count, 2)
        doc_text.insertTextContent.assert_any_call("tc1", image_instance, False)
        doc_text.insertTextContent.assert_any_call("tc2", image_instance, False)

    def test_insert_frame_uses_text_cursor(self):
        doc_text = MagicMock()
        frame_text_cursor = MagicMock(name="frame_cursor")
        doc_text.createTextCursorByRange.return_value = "frame-text-cursor"
        doc_text.insertTextContent = MagicMock()

        view_cursor = MagicMock(name="view_cursor")
        view_cursor.getStart.return_value = "range-start"
        view_cursor.jumpToStartOfPage = MagicMock()

        model = MagicMock()
        model.getText.return_value = doc_text

        text_frame_instance = MagicMock(name="text_frame")
        frame_text_obj = MagicMock(name="frame_text_obj")
        frame_text_obj.createTextCursor.return_value = frame_text_cursor
        frame_text_obj.insertString = MagicMock()
        text_frame_instance.getText.return_value = frame_text_obj

        model.createInstance.return_value = text_frame_instance

        image_instance = MagicMock(name="image_instance")

        image_tools._insert_frame(
            model,
            cursor=view_cursor,
            image=image_instance,
            width=10,
            height=20,
            title="hello",
        )

        doc_text.insertTextContent.assert_called_once_with("frame-text-cursor", text_frame_instance, False)
        text_frame_instance.insertTextContent.assert_called_once_with(frame_text_cursor, image_instance, False)
        frame_text_obj.insertString.assert_called_once_with(frame_text_cursor, "\nhello", False)


if __name__ == "__main__":
    unittest.main()

