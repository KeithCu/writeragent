import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_uno_mocks():
    """
    plugin/writer/image_tools.py imports several LibreOffice UNO types at module import time.
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
        def __init__(self, width=0, height=0):
            self.Width = width
            self.Height = height

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

from plugin.writer.images import image_tools  # noqa: E402


class TestShouldLinkImagePath(unittest.TestCase):
    def test_user_path_is_linked(self):
        # Must be outside tempfile.gettempdir() — default NamedTemporaryFile uses /tmp.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=os.getcwd()) as f:
            f.write(b"png")
            path = f.name
        try:
            self.assertTrue(image_tools._should_link_image_path(path))
        finally:
            os.unlink(path)

    def test_temp_path_is_embedded(self):
        with tempfile.NamedTemporaryFile(suffix=".png", dir=tempfile.gettempdir()) as f:
            self.assertFalse(image_tools._should_link_image_path(f.name))

    def test_cache_path_is_embedded(self):
        cache_dir = image_tools._image_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, "cached.png")
        with open(path, "wb") as f:
            f.write(b"x")
        try:
            self.assertFalse(image_tools._should_link_image_path(path))
        finally:
            os.unlink(path)


class TestWriterImageCursorConversion(unittest.TestCase):
    def _make_writer_model(self, image_instance):
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
        model.createInstance.return_value = image_instance
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
        return model, doc_text, text_cursor, view_cursor

    def test_insert_image_to_writer_uses_text_cursor(self):
        image_instance = MagicMock(name="image_instance")
        model, doc_text, text_cursor, _ = self._make_writer_model(image_instance)
        ctx = MagicMock()

        with patch.object(image_tools, "_should_link_image_path", return_value=False):
            image_tools._insert_image_to_writer(
                ctx,
                model,
                "/home/user/photo.png",
                width=10,
                height=20,
                title="t",
                description="d",
                add_frame=False,
            )

        doc_text.createTextCursorByRange.assert_called_once_with("range-start")
        doc_text.insertTextContent.assert_called_once_with(text_cursor, image_instance, False)
        image_instance.GraphicURL = "file:////home/user/photo.png"

    def test_insert_image_to_writer_linked_uses_dispatch(self):
        image_instance = MagicMock(name="linked_graphic")
        psi = MagicMock()
        psi.hasPropertyByName.return_value = True
        image_instance.getPropertySetInfo.return_value = psi
        model, doc_text, _, _ = self._make_writer_model(image_instance)
        ctx = MagicMock()

        with patch.object(image_tools, "_should_link_image_path", return_value=True):
            with patch.object(image_tools, "_dispatch_insert_linked_graphic", return_value=image_instance) as dispatch:
                image_tools._insert_image_to_writer(
                    ctx,
                    model,
                    "/home/user/photo.png",
                    width=10,
                    height=20,
                    title="t",
                    description="d",
                    add_frame=False,
                )

        dispatch.assert_called_once()
        doc_text.insertTextContent.assert_not_called()
        image_instance.setPropertyValue.assert_any_call("Width", 10)
        image_instance.setPropertyValue.assert_any_call("Height", 20)

    def test_dispatch_insert_linked_graphic_passes_as_link(self):
        ctx = MagicMock()
        model = MagicMock()
        frame = MagicMock()
        model.getCurrentController.return_value.getFrame.return_value = frame
        dispatcher = MagicMock()
        ctx.ServiceManager.createInstanceWithContext.return_value = dispatcher
        inserted = MagicMock()
        model.CurrentController.Selection.getCount.return_value = 1
        model.CurrentController.Selection.getByIndex.return_value = inserted
        inserted.supportsService.return_value = True

        result = image_tools._dispatch_insert_linked_graphic(ctx, model, "file:///home/user/photo.png")

        dispatcher.executeDispatch.assert_called_once()
        args = dispatcher.executeDispatch.call_args[0]
        self.assertEqual(args[1], ".uno:InsertGraphic")
        props = args[4]
        prop_map = {p.Name: p.Value for p in props}
        self.assertEqual(prop_map["FileName"], "file:///home/user/photo.png")
        self.assertTrue(prop_map["AsLink"])
        self.assertIs(result, inserted)

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
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
        ctx = MagicMock()

        doc_text.createTextCursorByRange.side_effect = ["tc1", "tc2"]

        with patch.object(image_tools, "_should_link_image_path", return_value=False):
            image_tools._insert_image_to_writer(
                ctx,
                model,
                "/tmp/generated.png",
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
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"

        text_frame_instance = MagicMock(name="text_frame")
        frame_text_obj = MagicMock(name="frame_text_obj")
        frame_text_obj.createTextCursor.return_value = frame_text_cursor
        frame_text_obj.insertString = MagicMock()
        text_frame_instance.getText.return_value = frame_text_obj

        image_instance = MagicMock(name="image_instance")
        model.createInstance.side_effect = [text_frame_instance, image_instance]
        ctx = MagicMock()

        with patch.object(image_tools, "_should_link_image_path", return_value=False):
            image_tools._insert_frame(
                ctx,
                model,
                "/tmp/img.png",
                width=10,
                height=20,
                title="hello",
                description="d",
            )

        doc_text.insertTextContent.assert_called_once_with("frame-text-cursor", text_frame_instance, False)
        text_frame_instance.insertTextContent.assert_called_once_with(frame_text_cursor, image_instance, False)
        frame_text_obj.insertString.assert_called_once_with(frame_text_cursor, "\nhello", False)


class TestReplaceGraphicSource(unittest.TestCase):
    def test_embed_path_sets_graphic_url(self):
        graphic = MagicMock(spec=["getPropertyValue", "setPropertyValue", "getPropertySetInfo"])
        graphic.getPropertyValue.return_value = MagicMock(Width=5000, Height=4000)
        psi = MagicMock()
        psi.hasPropertyByName.return_value = True
        graphic.getPropertySetInfo.return_value = psi
        model = MagicMock()
        model.supportsService.return_value = True
        ctx = MagicMock()

        with patch.object(image_tools, "_should_link_image_path", return_value=False):
            with patch.object(image_tools.uno, "systemPathToFileUrl", return_value="file:////tmp/new.png"):
                ok = image_tools.replace_graphic_source(ctx, model, graphic, "/tmp/new.png")

        self.assertTrue(ok)
        graphic.setPropertyValue.assert_any_call("GraphicURL", "file:////tmp/new.png")

    def test_link_path_dispatches_for_writer(self):
        graphic = MagicMock()
        graphic.getAnchor.return_value = MagicMock()
        graphic.getPropertyValue.return_value = MagicMock(Width=5000, Height=4000)
        model = MagicMock()
        model.getText.return_value = MagicMock()
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
        new_graphic = MagicMock()
        ctx = MagicMock()

        with patch.object(image_tools, "_should_link_image_path", return_value=True):
            with patch.object(image_tools, "_place_view_cursor_at_text_range"):
                with patch.object(image_tools, "_dispatch_insert_linked_graphic", return_value=new_graphic) as dispatch:
                    ok = image_tools.replace_graphic_source(ctx, model, graphic, "/home/user/new.png")

        self.assertTrue(ok)
        dispatch.assert_called_once()
        model.getText.return_value.removeTextContent.assert_called_once_with(graphic)


if __name__ == "__main__":
    unittest.main()
