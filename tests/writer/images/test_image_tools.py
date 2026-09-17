import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

# image_tools imports TextContentAnchorType at load time; conftest usually provides it.
_anchor_key = "com.sun.star.text.TextContentAnchorType"
if _anchor_key not in sys.modules:
    anchor_mod = types.ModuleType(_anchor_key)
    setattr(anchor_mod, "AS_CHARACTER", 1)
    setattr(anchor_mod, "AT_FRAME", 3)
    sys.modules[_anchor_key] = anchor_mod

# systemPathToFileUrl is used by image_tools; patch per-test where needed.
_uno = sys.modules.get("uno")
if isinstance(_uno, MagicMock):
    _uno.systemPathToFileUrl.side_effect = lambda p: f"file:///{p}"

from plugin.writer.images import image_tools  # noqa: E402


class TestInsertImageIntoHeaderFooter(unittest.TestCase):
    def test_enables_region_auto_height_and_embeds(self):
        model = MagicMock()
        style = MagicMock()
        region_text = MagicMock()
        cursor = MagicMock()
        graphic = MagicMock()
        graphic.getName.return_value = "Graphic1"

        style.getPropertyValue.side_effect = lambda name: {
            "HeaderIsOn": False,
            "HeaderText": region_text,
        }.get(name, MagicMock())
        region_text.createTextCursorByRange.return_value = cursor
        region_text.getEnd.return_value = MagicMock()

        with (
            patch("plugin.writer.page.resolve_page_style", return_value=(style, "Standard")),
            patch("plugin.writer.page.set_header_footer_auto_height") as set_auto,
            patch.object(image_tools, "_insert_embedded_at_writer_cursor", return_value=graphic) as insert,
        ):
            result = image_tools.insert_image_into_header_footer(
                model,
                "/tmp/logo.png",
                "header",
                width_mm=40,
                height_mm=20,
                auto_height=True,
            )

        style.setPropertyValue.assert_any_call("HeaderIsOn", True)
        set_auto.assert_called_once_with(style, "header", True)
        insert.assert_called_once()
        self.assertEqual(insert.call_args.kwargs.get("text_container"), region_text)
        self.assertEqual(result["style_name"], "Standard")
        self.assertEqual(result["region"], "header")
        self.assertTrue(result["auto_height"])
        self.assertIs(result["graphic"], graphic)

    def test_first_page_header_uses_header_text_first(self):
        # Shared HeaderText never reaches a different-first-page letterhead;
        # header_first must resolve HeaderTextFirst (same map as page_get/set).
        model = MagicMock()
        style = MagicMock()
        first_text = MagicMock(name="HeaderTextFirst")
        shared_text = MagicMock(name="HeaderText")
        cursor = MagicMock()
        graphic = MagicMock()

        style.getPropertyValue.side_effect = lambda name: {
            "HeaderIsOn": True,
            "HeaderText": shared_text,
            "HeaderTextFirst": first_text,
        }.get(name, MagicMock())
        first_text.createTextCursorByRange.return_value = cursor
        first_text.getEnd.return_value = MagicMock()

        with (
            patch("plugin.writer.page.resolve_page_style", return_value=(style, "Standard")),
            patch("plugin.writer.page.set_header_footer_auto_height") as set_auto,
            patch.object(image_tools, "_insert_embedded_at_writer_cursor", return_value=graphic) as insert,
        ):
            result = image_tools.insert_image_into_header_footer(
                model,
                "/tmp/logo.png",
                "header_first",
                width_mm=40,
                height_mm=20,
            )

        set_auto.assert_called_once_with(style, "header_first", True)
        self.assertEqual(insert.call_args.kwargs.get("text_container"), first_text)
        self.assertIsNot(insert.call_args.kwargs.get("text_container"), shared_text)
        self.assertEqual(result["region"], "header_first")

    def test_first_page_footer_uses_footer_text_first(self):
        model = MagicMock()
        style = MagicMock()
        first_text = MagicMock(name="FooterTextFirst")
        cursor = MagicMock()
        graphic = MagicMock()

        style.getPropertyValue.side_effect = lambda name: {
            "FooterIsOn": True,
            "FooterTextFirst": first_text,
        }.get(name, MagicMock())
        first_text.createTextCursorByRange.return_value = cursor
        first_text.getEnd.return_value = MagicMock()

        with (
            patch("plugin.writer.page.resolve_page_style", return_value=(style, "Standard")),
            patch("plugin.writer.page.set_header_footer_auto_height"),
            patch.object(image_tools, "_insert_embedded_at_writer_cursor", return_value=graphic) as insert,
        ):
            result = image_tools.insert_image_into_header_footer(
                model, "/tmp/logo.png", "footer_first",
            )

        self.assertEqual(insert.call_args.kwargs.get("text_container"), first_text)
        self.assertEqual(result["region"], "footer_first")

    def test_unknown_region_lists_page_keys(self):
        with self.assertRaises(ValueError) as raised:
            image_tools.insert_image_into_header_footer(
                MagicMock(), "/tmp/logo.png", "not_a_region",
            )
        self.assertIn("header_first", str(raised.exception))
        self.assertIn("footer_first", str(raised.exception))


class TestShouldLinkImagePath(unittest.TestCase):
    def test_user_path_is_linked(self):
        # What was wrong: in make release, tests run from a tempdir under /tmp ($RELEASE_TMP),
        # so os.getcwd() was inside tempfile.gettempdir(), causing _should_link_image_path to return False.
        # This change mocks gettempdir to a distinct path to ensure hermetic testing.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"png")
            path = f.name
        try:
            with patch("tempfile.gettempdir", return_value="/nonexistent/custom_temp_dir"):
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
        view_cursor.getText.return_value = doc_text
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

        doc_text.createTextCursorByRange.assert_called_once_with(model.CurrentController.ViewCursor)
        doc_text.insertTextContent.assert_called_once_with(text_cursor, image_instance, False)
        image_instance.GraphicURL = "file:////home/user/photo.png"

    def test_insert_uses_cell_xtext_not_body_clone(self):
        """Cursor in a nested XText must not clone through the body."""
        body = MagicMock()
        body.createTextCursorByRange.side_effect = RuntimeError(
            "End of content node doesn't have the proper start node"
        )
        cell_text = MagicMock()
        text_cursor = MagicMock(name="cell_cursor")
        cell_text.createTextCursorByRange.return_value = text_cursor
        cell_text.insertTextContent = MagicMock()

        view_cursor = MagicMock(name="view_cursor")
        view_cursor.getText.return_value = cell_text
        view_cursor.jumpToStartOfPage = MagicMock()

        image_instance = MagicMock(name="image_instance")
        model = MagicMock()
        model.getText.return_value = body
        model.CurrentController = MagicMock()
        model.CurrentController.ViewCursor = view_cursor
        model.createInstance.return_value = image_instance
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
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

        body.createTextCursorByRange.assert_not_called()
        cell_text.createTextCursorByRange.assert_called_once_with(view_cursor)
        cell_text.insertTextContent.assert_called_once_with(text_cursor, image_instance, False)
        view_cursor.jumpToStartOfPage.assert_not_called()

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
        from plugin.doc import visual_helpers

        ctx = MagicMock()
        model = MagicMock()
        frame = MagicMock()
        controller = MagicMock()
        controller.getFrame.return_value = frame
        selection = MagicMock()
        selection.getCount.return_value = 1
        inserted = MagicMock()
        selection.getByIndex.return_value = inserted
        controller.getSelection.return_value = selection
        model.getCurrentController.return_value = controller
        dispatcher = MagicMock()
        ctx.ServiceManager.createInstanceWithContext.return_value = dispatcher
        inserted.supportsService.side_effect = lambda svc: svc == visual_helpers.WRITER_GRAPHIC_SERVICE

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
        view_cursor.getText.return_value = doc_text
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
        view_cursor.getText.return_value = doc_text
        view_cursor.jumpToStartOfPage = MagicMock()

        model = MagicMock()
        model.getText.return_value = doc_text
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
        model.CurrentController = MagicMock()
        model.CurrentController.ViewCursor = view_cursor

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


class TestImageCompoundUndo(unittest.TestCase):
    def test_insert_image_groups_undo_before_gallery(self):
        events: list[object] = []

        class FakeUndo:
            def __init__(self, doc, title):
                events.append(("enter", title))
                self.title = title

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()

            def close(self):
                events.append(("close", self.title))

        model = MagicMock()
        ctx = MagicMock()
        with (
            patch.object(image_tools, "get_type_doc", return_value="writer"),
            patch("plugin.doc.visual_helpers.px_to_display_units", return_value=(1000, 1000)),
            patch.object(
                image_tools,
                "_insert_image_to_writer",
                side_effect=lambda *a, **k: events.append("insert"),
            ),
            patch.object(
                image_tools,
                "add_image_to_gallery",
                side_effect=lambda *a, **k: events.append("gallery"),
            ),
            patch("plugin.writer.edit_review.WriterCompoundUndo", FakeUndo),
        ):
            image_tools.insert_image(
                ctx, model, "/tmp/x.png", 64, 64, add_to_gallery=True, add_frame=False,
            )

        self.assertEqual(
            events,
            [
                ("enter", "WriterAgent: Insert image"),
                "insert",
                ("close", "WriterAgent: Insert image"),
                "gallery",
            ],
        )

    def test_replace_graphic_source_groups_undo(self):
        events: list[object] = []

        class FakeUndo:
            def __init__(self, doc, title):
                events.append(("enter", title))
                self.title = title

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()

            def close(self):
                events.append(("close", self.title))

        graphic = MagicMock()
        graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
        model = MagicMock()
        model.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
        ctx = MagicMock()
        with (
            patch.object(image_tools, "get_type_doc", return_value="writer"),
            patch.object(image_tools, "_should_link_image_path", return_value=False),
            patch.object(image_tools, "_safe_set_property", return_value=True),
            patch("plugin.writer.edit_review.WriterCompoundUndo", FakeUndo),
        ):
            ok = image_tools.replace_graphic_source(ctx, model, graphic, "/tmp/cache/x.png")

        self.assertTrue(ok)
        self.assertEqual(
            events,
            [
                ("enter", "WriterAgent: Replace image"),
                ("close", "WriterAgent: Replace image"),
            ],
        )


class TestDisplaySizeCap(unittest.TestCase):
    def test_insert_image_caps_1024_to_135mm(self):
        from plugin.doc import visual_helpers

        captured: dict[str, int] = {}

        def _capture(_ctx, _model, _path, width, height, *_args, **_kwargs):
            captured["width"] = width
            captured["height"] = height

        model = MagicMock()
        ctx = MagicMock()
        with (
            patch.object(image_tools, "get_type_doc", return_value="writer"),
            patch.object(image_tools, "_insert_image_to_writer", side_effect=_capture),
            patch("plugin.writer.edit_review.WriterCompoundUndo"),
        ):
            image_tools.insert_image(
                ctx, model, "/tmp/x.png", 1024, 1024, add_to_gallery=False, add_frame=False,
            )

        expected = visual_helpers.px_to_display_units(1024, 1024)
        self.assertEqual((captured["width"], captured["height"]), expected)
        self.assertLessEqual(max(captured["width"], captured["height"]), 13500)
        raw_w, raw_h = visual_helpers.px_to_units(1024, 1024)
        self.assertGreater(max(raw_w, raw_h), 13500)

    def test_replace_image_in_place_caps_1024_to_135mm(self):
        from plugin.doc import visual_helpers

        graphic = MagicMock()
        captured: dict[str, int] = {}

        def _capture(_ctx, _model, _graphic, _path, width_units=None, height_units=None, **_kwargs):
            captured["width"] = width_units
            captured["height"] = height_units
            return True

        model = MagicMock()
        ctx = MagicMock()
        with (
            patch.object(image_tools, "_get_selected_graphic_object", return_value=(graphic, "writer")),
            patch.object(image_tools, "replace_graphic_source", side_effect=_capture),
        ):
            ok = image_tools.replace_image_in_place(
                ctx, model, "/tmp/x.png", 1536, 768, add_to_gallery=False,
            )

        self.assertTrue(ok)
        expected = visual_helpers.px_to_display_units(1536, 768)
        self.assertEqual((captured["width"], captured["height"]), expected)
        self.assertEqual(captured["width"], 13500)


class TestDrawPageInsertPosition(unittest.TestCase):
    def test_centers_when_xy_omitted(self):
        page = MagicMock()
        page.Width = 28000
        page.Height = 15750
        pos = image_tools._position_on_draw_page(page, 8000, 4000, None, None)
        self.assertEqual(pos.X, (28000 - 8000) // 2)
        self.assertEqual(pos.Y, (15750 - 4000) // 2)

    def test_explicit_mm(self):
        page = MagicMock()
        page.Width = 28000
        page.Height = 15750
        pos = image_tools._position_on_draw_page(page, 8000, 4000, 30, 40)
        self.assertEqual(pos.X, 3000)
        self.assertEqual(pos.Y, 4000)


if __name__ == "__main__":
    unittest.main()
