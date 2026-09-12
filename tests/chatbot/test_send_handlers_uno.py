# WriterAgent - Native UNO tests for sidebar Image-mode send
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headed proof: Image mode forwards source_image='selection' when a graphic is selected.

Does not exercise the images specialist / #757 prompt-steer path.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from plugin.chatbot.send_handlers import SendHandlersMixin, _direct_image_source_arg
from plugin.framework.async_stream import StreamQueueKind
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.images.image_tools import insert_image_at_locator


def _logo_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("extension/assets/logo_32.png", "assets/logo_32.png"):
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path):
            return path
    return os.path.join(root, "extension", "assets", "logo_32.png")


def _insert_and_select_graphic(ctx, doc):
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    graphic = insert_image_at_locator(ctx, doc, logo, width_mm=20, height_mm=20)
    assert graphic is not None, "failed to insert fixture image"
    doc.getCurrentController().select(graphic)
    return graphic


class _DirectImageHost(SendHandlersMixin):
    def __init__(self, ctx):
        self.ctx = ctx
        self.stop_requested = False
        self._in_librarian_mode = False
        self.responses = []
        self.status_history = []
        self._terminal_status = None
        self._record_assistant_start = False
        self.session = MagicMock()
        self.response_control = MagicMock()
        self.aspect_ratio_selector = MagicMock()
        self.aspect_ratio_selector.getText.return_value = "Square"
        self.image_model_selector = MagicMock()
        self.image_model_selector.getText.return_value = "dall-e-3"
        self.base_size_input = MagicMock()
        self.base_size_input.getText.return_value = "512"

    def _append_response(self, text, role="assistant"):
        self.responses.append(text)

    def _set_status(self, text):
        self.status_history.append(text)

    def _get_doc_type_str(self, model):
        return "Writer"

    def resolve_stop_checker(self):
        return lambda: self.stop_requested

    def rerender_rich_text_session(self):
        pass


def _drive_direct_image_send(host, doc, execute_return):
    """Run ``_do_send_direct_image`` with mocked tools; live selection is not stubbed."""
    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = execute_return
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    with patch.dict("sys.modules", {"plugin.main": mock_main}):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:

            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:

                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, ctx, stop_checker, **kwargs):
                    while not q.empty():
                        item = q.get()
                        k = item[0]
                        if k == StreamQueueKind.CHUNK:
                            apply_chunk(item[1])
                        elif k == StreamQueueKind.STREAM_DONE:
                            on_stream_done(item)
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])
                    job_done[0] = True

                mock_run_stream.side_effect = fake_drain_loop
                host._do_send_direct_image("make it look like a wizard", doc)

    return mock_registry


@native_test
@with_native_doc("writer")
def test_direct_image_source_arg_none_without_graphic(ctx, doc):
    assert _direct_image_source_arg(doc) is None


@native_test
@with_native_doc("writer")
def test_direct_image_source_arg_selection_when_graphic_selected(ctx, doc):
    _insert_and_select_graphic(ctx, doc)
    assert _direct_image_source_arg(doc) == "selection"


@native_test
@with_native_doc("writer")
def test_do_send_direct_image_omits_source_image_without_selection(ctx, doc):
    host = _DirectImageHost(ctx)
    registry = _drive_direct_image_send(
        host, doc, {"status": "done", "message": "Image generated successfully"}
    )
    registry.execute.assert_called_once()
    kwargs = registry.execute.call_args.kwargs
    assert kwargs["prompt"] == "make it look like a wizard"
    assert "source_image" not in kwargs


@native_test
@with_native_doc("writer")
def test_do_send_direct_image_passes_source_image_when_graphic_selected(ctx, doc):
    _insert_and_select_graphic(ctx, doc)
    host = _DirectImageHost(ctx)
    registry = _drive_direct_image_send(
        host, doc, {"status": "done", "message": "Image edited successfully"}
    )
    registry.execute.assert_called_once()
    args, kwargs = registry.execute.call_args
    assert args[0] == "image_generate"
    assert kwargs["prompt"] == "make it look like a wizard"
    assert kwargs["source_image"] == "selection"
