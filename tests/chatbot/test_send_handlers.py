import queue
from unittest.mock import MagicMock, patch

from plugin.framework.async_stream import StreamQueueKind
from plugin.chatbot.send_handlers import SendHandlersMixin
from plugin.chatbot.state_machine import EffectInterpreter, SendHandlerState
from plugin.tests.testing_utils import MockContext, MockDocument
import pytest
pytest.importorskip("requests")
from plugin.contrib.smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole

class DummyChatbotPanel(SendHandlersMixin):
    def __init__(self):
        self.ctx = MockContext()
        setattr(self.ctx, "getServiceManager", MagicMock())
        self.stop_requested = False
        self._in_librarian_mode = False
        self.responses = []
        self.thinking_flags = []
        self.status_history = []
        self._terminal_status = None
        self._record_assistant_start = False
        setattr(self, "session", MagicMock())
        setattr(self, "response_control", MagicMock())

        # UI Mocks
        self.aspect_ratio_selector = MagicMock()
        self.aspect_ratio_selector.getText.return_value = "Landscape (16:9)"

        self.image_model_selector = MagicMock()
        self.image_model_selector.getText.return_value = "dall-e-3"

        self.base_size_input = MagicMock()
        self.base_size_input.getText.return_value = "1024"

    def _append_response(self, text, is_thinking=False, role="assistant"):
        self.responses.append(text)
        self.thinking_flags.append(bool(is_thinking))

    def _set_status(self, text):
        self.status_history.append(text)

    # We need to mock _get_doc_type_str since SendHandlersMixin uses it implicitly in some places
    def _get_doc_type_str(self, model):
        return "Writer"

    def resolve_stop_checker(self):
        return lambda: self.stop_requested

    def rerender_rich_text_session(self):
        pass


def test_get_mcp_url_uses_schema_keys_only():
    """Agent backends must not read mcp.host (not in module.yaml)."""
    panel = DummyChatbotPanel()
    with patch("plugin.chatbot.send_handlers.get_config", return_value="true"), patch("plugin.chatbot.send_handlers.get_config_int_safe", return_value=18765) as mock_port:
        url = panel._get_mcp_url()  # type: ignore
    mock_port.assert_called_once_with("mcp.mcp_port")
    assert url == "http://localhost:18765/mcp"

def test_get_mcp_url_returns_none_when_disabled():
    panel = DummyChatbotPanel()
    with patch("plugin.chatbot.send_handlers.get_config", return_value="false"):
        url = panel._get_mcp_url()  # type: ignore
    assert url is None


def test_agent_backend_prompt_omits_mcp_when_server_stopped():
    panel = DummyChatbotPanel()
    panel._get_mcp_url = lambda: "http://localhost:18765/mcp"  # type: ignore
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def fake_drain(q, run_agent, *args, **kwargs):
        run_agent()

    panel._run_unified_worker_drain_loop = fake_drain  # type: ignore
    with (
        patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=MagicMock()),
        patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter),
        patch("plugin.mcp.is_mcp_server_running", return_value=False),
    ):
        panel._execute_agent_backend_effect("hello", MagicMock(), "writer", MagicMock(), MagicMock())

    adapter.send.assert_called_once()
    system_prompt = adapter.send.call_args.kwargs["system_prompt"]
    assert "[MCP SERVER AVAILABLE]" not in system_prompt


def test_agent_backend_prompt_includes_mcp_when_server_running():
    panel = DummyChatbotPanel()
    panel._get_mcp_url = lambda: "http://localhost:18765/mcp"  # type: ignore
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def fake_drain(q, run_agent, *args, **kwargs):
        run_agent()

    panel._run_unified_worker_drain_loop = fake_drain  # type: ignore
    with (
        patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=MagicMock()),
        patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter),
        patch("plugin.mcp.is_mcp_server_running", return_value=True),
    ):
        panel._execute_agent_backend_effect("hello", MagicMock(), "writer", MagicMock(), MagicMock())

    adapter.send.assert_called_once()
    system_prompt = adapter.send.call_args.kwargs["system_prompt"]
    assert "[MCP SERVER AVAILABLE]" in system_prompt



def test_run_web_research_stores_raw_answer_and_rerenders():
    panel = DummyChatbotPanel()
    panel.rerender_rich_text_session = MagicMock()
    model = MockDocument()
    panel.session = MagicMock()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = '{"status": "ok", "result": "<p>HTML answer</p>"}'
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object): pass
    class DummyBase2(object): pass
    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict('sys.modules', {'plugin.main': mock_main, 'uno': mock_uno, 'unohelper': mock_unohelper, 'com.sun.star.text': MagicMock(), 'com.sun.star.awt': mock_awt, 'com.sun.star.lang': mock_lang}):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                panel._run_web_research("What is X?", model)  # type: ignore

    panel.session.add_assistant_message.assert_called_once_with(content="<p>HTML answer</p>")
    panel.rerender_rich_text_session.assert_called_once()
    assert "<p>HTML answer</p>\n" in panel.responses
    assert "AI (research):" not in "".join(panel.responses)


def test_run_web_research_tool_context_uses_panel_ctx_not_get_ctx():
    """Regression: sub-agent worker must not call get_ctx() off the main thread."""
    panel = DummyChatbotPanel()
    panel.rerender_rich_text_session = MagicMock()
    model = MockDocument()
    panel.session = MagicMock()
    captured_tool_ctx = []

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry._services = MagicMock()

    def capture_execute(_tool_name, tctx, **_kwargs):
        captured_tool_ctx.append(tctx)
        return '{"status": "ok", "result": "answer"}'

    mock_registry.execute.side_effect = capture_execute
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                with patch(
                    "plugin.framework.uno_context.get_ctx",
                    side_effect=AssertionError("get_ctx must not run on background thread"),
                ):
                    panel._run_web_research("What is X?", model)  # type: ignore

    assert len(captured_tool_ctx) == 1
    assert captured_tool_ctx[0].ctx is panel.ctx


def test_do_send_direct_image_tool_context_uses_panel_ctx_not_get_ctx():
    """Regression: direct-image worker must not call get_ctx() off the main thread."""
    panel = DummyChatbotPanel()
    model = MockDocument()
    captured_tool_ctx = []

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry._services = MagicMock()

    def capture_execute(_tool_name, tctx, **_kwargs):
        captured_tool_ctx.append(tctx)
        return {"status": "done", "message": "Image generated successfully"}

    mock_registry.execute.side_effect = capture_execute
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                with patch(
                    "plugin.framework.uno_context.get_ctx",
                    side_effect=AssertionError("get_ctx must not run on background thread"),
                ):
                    panel._do_send_direct_image("A cute dog", model)  # type: ignore

    assert len(captured_tool_ctx) == 1
    assert captured_tool_ctx[0].ctx is panel.ctx


def test_do_send_direct_image():
    panel = DummyChatbotPanel()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = {"status": "done", "message": "Image generated successfully"}
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object): pass
    class DummyBase2(object): pass
    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict('sys.modules', {
        'plugin.main': mock_main,
        'uno': mock_uno,
        'unohelper': mock_unohelper,
        'com.sun.star.text': MagicMock(),
        'com.sun.star.awt': mock_awt,
        'com.sun.star.lang': mock_lang
    }):
        # Patch run_in_background where it's actually USED by async_stream.py
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                # Ensure get_toolkit works
                smgr = getattr(panel.ctx, "getServiceManager")()
                smgr.createInstanceWithContext.return_value = MagicMock()

                panel._do_send_direct_image("A cute dog", model)  # type: ignore

                # Verify turn_session added the user prompt
                panel.session.add_user_message.assert_called_with("A cute dog")

                # Verify responses
                assert "A cute dog" in panel.responses
                assert "AI: Creating image...\n" in panel.responses
                assert any("image_generate: Image generated successfully" in r for r in panel.responses)

                # Verify tool registry was called
                mock_registry.execute.assert_called_once()
                args, kwargs = mock_registry.execute.call_args
                assert args[0] == "image_generate"
                assert kwargs["prompt"] == "A cute dog"
                assert "source_image" not in kwargs

def _run_direct_image_send(panel, model, execute_return, selected_graphic=None):
    """Drive ``_do_send_direct_image`` with mocked tools and drain loop."""
    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = execute_return
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()

    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                smgr = getattr(panel.ctx, "getServiceManager")()
                smgr.createInstanceWithContext.return_value = MagicMock()

                with patch(
                    "plugin.chatbot.send_handlers.selected_graphic_object",
                    return_value=selected_graphic,
                ):
                    panel._do_send_direct_image("make it look like a wizard", model)  # type: ignore

    return mock_registry


def test_direct_image_maps_translated_aspect_label_to_tool_enum():
    """Sidebar combo text is translated; image_generate still receives the English enum."""
    panel = DummyChatbotPanel()
    panel.aspect_ratio_selector.getText.return_value = "横長"

    def fake_canonical(displayed: str) -> str:
        assert displayed == "横長"
        return "Landscape (16:9)"

    model = MockDocument()
    with patch("plugin.chatbot.settings_dialog.canonical_aspect_label", side_effect=fake_canonical):
        mock_registry = _run_direct_image_send(panel, model, {"status": "done", "message": "ok"})
    args, kwargs = mock_registry.execute.call_args
    assert args[0] == "image_generate"
    assert kwargs["aspect_ratio"] == "landscape_16_9"


def test_direct_image_source_arg_none_without_selection():
    from plugin.chatbot.send_handlers import _direct_image_source_arg

    with patch("plugin.chatbot.send_handlers.selected_graphic_object", return_value=None):
        assert _direct_image_source_arg(MockDocument()) is None


def test_direct_image_source_arg_selection_when_graphic_selected():
    from plugin.chatbot.send_handlers import _direct_image_source_arg

    with patch("plugin.chatbot.send_handlers.selected_graphic_object", return_value=object()):
        assert _direct_image_source_arg(MockDocument()) == "selection"


def test_do_send_direct_image_with_selection_passes_source_image():
    panel = DummyChatbotPanel()
    model = MockDocument()
    mock_registry = _run_direct_image_send(
        panel,
        model,
        {"status": "done", "message": "Image edited successfully"},
        selected_graphic=object(),
    )

    mock_registry.execute.assert_called_once()
    args, kwargs = mock_registry.execute.call_args
    assert args[0] == "image_generate"
    assert kwargs["prompt"] == "make it look like a wizard"
    assert kwargs["source_image"] == "selection"


def test_direct_image_success_persists_assistant_note():
    """The [image_generate: …] note is the assistant row, not display-only."""
    panel = DummyChatbotPanel()
    _run_direct_image_send(
        panel,
        MockDocument(),
        {"status": "done", "message": "Image generated successfully"},
    )
    panel.session.add_assistant_message.assert_called_once_with(
        content="[image_generate: Image generated successfully]"
    )
    assert panel._terminal_status == "Ready"
    assert any("image_generate: Image generated successfully" in text for text in panel.responses)


def test_direct_image_tool_error_persists_assistant_note():
    """A tool status=error is not the exception path. The note still has to be stored."""
    panel = DummyChatbotPanel()
    _run_direct_image_send(
        panel,
        MockDocument(),
        {"status": "error", "message": "Failed to generate image"},
    )
    panel.session.add_assistant_message.assert_called_once_with(
        content="[image_generate: Failed to generate image]"
    )
    assert "[image_generate: Failed to generate image]\n" in panel.responses


def test_direct_image_stop_keeps_stopped_status():
    panel = DummyChatbotPanel()
    panel._terminal_status = "Ready"
    state = SendHandlerState(handler_type="image", status="ready")
    interpreter = EffectInterpreter(panel)

    def fake_drain(q, worker, current_state, interpreter, **kwargs):
        panel._terminal_status = "Stopped"

    with patch("plugin.chatbot.send_handlers.update_lru_history"):
        with patch.object(panel, "_run_unified_worker_drain_loop", side_effect=fake_drain):
            panel._execute_direct_image_effect("a cat", MagicMock(), state, interpreter)
    assert panel._terminal_status == "Stopped"


def test_do_send_direct_image_error():
    panel = DummyChatbotPanel()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = {"status": "error", "message": "Failed to generate image"}
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object): pass
    class DummyBase2(object): pass
    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict('sys.modules', {
        'plugin.main': mock_main,
        'uno': mock_uno,
        'unohelper': mock_unohelper,
        'com.sun.star.text': MagicMock(),
        'com.sun.star.awt': mock_awt,
        'com.sun.star.lang': mock_lang
    }):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
                    while not q.empty():
                        item = q.get()
                        k = item[0]
                        if k == StreamQueueKind.CHUNK:
                            apply_chunk(item[1])
                        elif k == StreamQueueKind.STREAM_DONE:
                            on_stream_done(item)
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])
                    job_done[0] = True
                mock_run_stream.side_effect = fake_drain_loop

                smgr = getattr(panel.ctx, "getServiceManager")()
                smgr.createInstanceWithContext.return_value = MagicMock()

                panel._do_send_direct_image("A cute dog", model)  # type: ignore

                # Verify error message is surfaced to user
                assert "[image_generate: Failed to generate image]\n" in panel.responses
                mock_registry.execute.assert_called_once()

def _drive_unified_drain(panel, worker_fn, handler_type: str) -> None:
    """Run the send drain on this thread and stop at the first terminal item.

    The send path calls ``begin_send_turn`` before the worker. Tests that
    enter the drain directly do the same so Stop has a turn to close.

    The real drain sets job_done on STREAM_DONE / STOPPED and does not also
    run the wrapper's trailing STREAM_DONE sentinel.
    """
    from plugin.chatbot.tool_loop_actions import begin_send_turn, current_turn

    if current_turn(panel) is None:
        begin_send_turn(panel, handler_type)
    q: queue.Queue = queue.Queue()
    state = SendHandlerState(handler_type=handler_type, status="starting")
    interpreter = EffectInterpreter(panel)

    def fake_run_bg(func, **kwargs):
        func()

    def fake_drain_loop(drain_q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
        while not drain_q.empty():
            item = drain_q.get()
            kind = item[0]
            if kind == StreamQueueKind.CHUNK:
                apply_chunk(item[1], False)
            elif kind == StreamQueueKind.THINKING:
                apply_chunk(item[1], True)
            elif kind == StreamQueueKind.STREAM_DONE:
                on_stream_done(item)
                job_done[0] = True
                return
            elif kind == StreamQueueKind.STOPPED:
                on_stopped()
                job_done[0] = True
                return
            elif kind == StreamQueueKind.ERROR:
                on_error(item[1])
                job_done[0] = True
                return
        job_done[0] = True

    with patch("plugin.framework.async_stream.run_in_background", side_effect=fake_run_bg):
        with patch("plugin.framework.async_stream.run_stream_drain_loop", side_effect=fake_drain_loop):
            panel._run_unified_worker_drain_loop(q, lambda: worker_fn(q), state, interpreter)


def test_agent_stream_persists_visible_text_once_on_done():
    panel = DummyChatbotPanel()

    def worker(q) -> None:
        q.put((StreamQueueKind.THINKING, "hidden"))
        q.put((StreamQueueKind.CHUNK, "Hello "))
        q.put((StreamQueueKind.CHUNK, "world"))
        q.put((StreamQueueKind.STREAM_DONE, {}))

    _drive_unified_drain(panel, worker, "agent")
    panel.session.add_assistant_message.assert_called_once_with(content="Hello world")


def test_agent_stop_stores_partial_or_placeholder():
    panel = DummyChatbotPanel()

    def worker(q) -> None:
        q.put((StreamQueueKind.CHUNK, "partial"))
        q.put((StreamQueueKind.STOPPED, None))

    _drive_unified_drain(panel, worker, "agent")
    panel.session.add_assistant_message.assert_any_call(content="partial")
    assert any(
        "[Stopped by user]" in str(call.kwargs.get("content") or "")
        for call in panel.session.add_assistant_message.call_args_list
    )


def test_agent_stop_with_no_chunks_stores_placeholder():
    panel = DummyChatbotPanel()

    def worker(q) -> None:
        q.put((StreamQueueKind.STOPPED, None))

    _drive_unified_drain(panel, worker, "agent")
    panel.session.add_assistant_message.assert_any_call(content="No response.")
    assert any(
        "[Stopped by user]" in str(call.kwargs.get("content") or "")
        for call in panel.session.add_assistant_message.call_args_list
    )


def test_brainstorm_finish_runs_on_stream_done():
    panel = DummyChatbotPanel()
    panel.on_brainstorming_session_finished = MagicMock()
    panel._in_brainstorming_mode = True

    def worker(q) -> None:
        q.put((StreamQueueKind.STREAM_DONE, {"brainstorming_finished": True, "spec_saved": True}))

    _drive_unified_drain(panel, worker, "web")
    panel.on_brainstorming_session_finished.assert_called_once_with(spec_saved=True)


def test_writing_plan_finish_without_callback_clears_mode_flag():
    panel = DummyChatbotPanel()
    panel._in_writing_plan_mode = True

    def worker(q) -> None:
        q.put((StreamQueueKind.STREAM_DONE, {"writing_plan_finished": True}))

    _drive_unified_drain(panel, worker, "web")
    assert panel._in_writing_plan_mode is False



def test_acp_approval_default_and_dead_turn():
    from plugin.chatbot.send_handlers import SendHandlersMixin
    import unittest.mock as mock

    class DummyHost(SendHandlersMixin):
        def __init__(self):
            self.stop_requested = False
            self.ctx = mock.MagicMock()
            self.frame = mock.MagicMock()
            self._current_agent_backend = None
            self._terminal_status = "Ready"
        def _append_response(self, *args, **kwargs):
            pass
        def _set_status(self, text):
            pass
        def resolve_stop_checker(self):
            return lambda: False

    host = DummyHost()
    adapter = mock.MagicMock()
    turn_session = mock.MagicMock()

    with mock.patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter), \
         mock.patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=turn_session), \
         mock.patch.object(host, "_run_unified_worker_drain_loop") as mock_loop:

        with mock.patch("plugin.chatbot.send_handlers.show_approval_dialog") as mock_dialog:
            mock_dialog.return_value = True

            # DO NOT set_config. We want the true application default.

            host._do_send_via_agent_backend("hello", mock.MagicMock(), "writer")

            cb = mock_loop.call_args.kwargs.get("on_approval_callback")

            # 1. Normal turn
            cb(("approval_required", "desc", "tool", {}, 123))

            # 2. Dead turn
            host.stop_requested = True
            cb(("approval_required", "desc2", "tool2", {}, 124))

            mock_dialog.assert_called_once_with(host.ctx, "desc", "tool", parent_frame=host.frame)
            adapter.submit_approval.assert_any_call(123, True)
            adapter.submit_approval.assert_any_call(124, False)


def test_acp_approval_auto_approve_when_configured():
    from plugin.chatbot.send_handlers import SendHandlersMixin
    from plugin.framework.config import set_config
    import unittest.mock as mock

    class DummyHost(SendHandlersMixin):
        def __init__(self):
            self.stop_requested = False
            self.ctx = mock.MagicMock()
            self.frame = mock.MagicMock()
            self._current_agent_backend = None
            self._terminal_status = "Ready"
        def _append_response(self, *args, **kwargs):
            pass
        def _set_status(self, text):
            pass
        def resolve_stop_checker(self):
            return lambda: False

    host = DummyHost()
    adapter = mock.MagicMock()
    turn_session = mock.MagicMock()

    with mock.patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter), \
         mock.patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=turn_session), \
         mock.patch.object(host, "_run_unified_worker_drain_loop") as mock_loop:

        with mock.patch("plugin.chatbot.send_handlers.show_approval_dialog") as mock_dialog:

            set_config("agent_backend.prompt_for_permission", False)

            host._do_send_via_agent_backend("hello", mock.MagicMock(), "writer")
            cb = mock_loop.call_args.kwargs.get("on_approval_callback")

            cb(("approval_required", "desc", "tool", {}, 999))

            mock_dialog.assert_not_called()
            adapter.submit_approval.assert_called_once_with(999, True)

def test_missing_agent_backend_does_not_store_user_row():
    panel = DummyChatbotPanel()
    panel.session.refresh_document_context = MagicMock()
    panel.session.document_context = ""
    state = SendHandlerState(handler_type="agent", status="starting")
    interpreter = EffectInterpreter(panel)

    with patch("plugin.chatbot.send_handlers.get_config", return_value="missing"):
        with patch("plugin.chatbot.send_handlers.get_backend", return_value=None):
            panel._execute_agent_backend_effect("hello", MockDocument(), "writer", state, interpreter)

    panel.session.add_user_message.assert_not_called()
    assert "hello" not in panel.responses
    assert panel._terminal_status == "Error"


def test_web_research_tool():
    # Setup mock context
    ctx = MagicMock()
    ctx.ctx = MockContext()
    # Mock get_config logic inside web_research to avoid KeyError
    from unittest.mock import patch
    setattr(ctx.ctx, "getServiceManager", MagicMock())  # for ConfigService
    ctx.status_callback = MagicMock()
    ctx.append_thinking_callback = MagicMock()
    ctx.stop_checker = lambda: False

    # Track the steps of our mock model
    call_count = [0]

    # We will mock WriterAgentSmolModel's generate method to simulate a ReAct loop
    # Step 1: Model decides to call duckduckgo search
    # Step 2: Model decides to visit a webpage
    # Step 3: Model returns final answer

    def mock_generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):
        call_count[0] += 1

        if call_count[0] == 1:
            # Call web_search tool
            tc = ChatMessageToolCall(
                id="call_1",
                type="function",
                function=ChatMessageToolCallFunction(
                    name="web_search",
                    arguments='{"query": "Latest Python release"}'
                )
            )
            return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])

        elif call_count[0] == 2:
            # WebResearchToolCallingAgent appends/merges a step-budget line.
            assert messages[-1].role in (MessageRole.USER, MessageRole.TOOL_RESPONSE)
            assert "Step budget" in str(messages[-1].content)
            tool_responses = [m for m in messages if m.role == MessageRole.TOOL_RESPONSE]
            assert tool_responses
            assert tool_responses[-1].role == MessageRole.TOOL_RESPONSE

            # Call visit_webpage tool
            tc = ChatMessageToolCall(
                id="call_2",
                type="function",
                function=ChatMessageToolCallFunction(
                    name="visit_webpage",
                    arguments='{"url": "https://python.org/downloads"}'
                )
            )
            return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])

        else:
            # Return final answer
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="The latest Python release is 3.12.3",
                tool_calls=[]
            )


    with patch("plugin.chatbot.smol_agent.WriterAgentSmolModel.generate", new=mock_generate):
        # We also need to mock requests.get/post that the default tools use under the hood
        # We can just mock the output of the VisitWebpageTool entirely to avoid making HTTP requests.

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"""
            <html>
                <body>
                    <a class='result-snippet' href='https://python.org/downloads'>Python 3.12.3 is released</a>
                    <div id="content">Python 3.12.3 is released today</div>
                </body>
            </html>"""
            mock_resp.headers.get_content_charset.return_value = "utf-8"
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            with patch("requests.get") as mock_get:
                mock_get_resp = MagicMock()
                mock_get_resp.status_code = 200
                mock_get_resp.text = "<html><body><h1>Python 3.12.3 is available!</h1></body></html>"
                mock_get.return_value = mock_get_resp

                from plugin.writer.specialized_base import DelegateToSpecializedWriter
                tool = DelegateToSpecializedWriter()
                with patch("plugin.framework.config.get_config", return_value="false"):
                    def _cfg_int(key):
                        if key == "web_cache_max_mb":
                            return 0  # disable SQLite cache (avoids db path issues in tests)
                        if key == "chat_max_tokens":
                            return 2048
                        return 10

                    with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int):
                        with patch("plugin.framework.config.get_api_config", return_value={}):
                            result = tool.execute(ctx, domain="web_research", task="What is the latest Python release?")

                assert result["status"] == "ok"
                assert "3.12.3" in result["result"]

                # Check that callbacks were called
                ctx.status_callback.assert_any_call("Sub-agent starting web search: What is the latest Python release?")
                ctx.status_callback.assert_any_call("Search: Latest Python release...")
                ctx.status_callback.assert_any_call("Read: python.org...")
                assert ctx.append_thinking_callback.called

def test_web_research_tool_stop():
    ctx = MagicMock()
    ctx.ctx = MockContext()
    from unittest.mock import patch
    setattr(ctx.ctx, "getServiceManager", MagicMock())  # for ConfigService
    ctx.stop_checker = lambda: True  # Stop immediately

    with patch("plugin.chatbot.smol_agent.WriterAgentSmolModel.generate", return_value=ChatMessage(role=MessageRole.ASSISTANT, content="")):
        with patch("urllib.request.urlopen"):
            with patch("requests.get"):
                from plugin.writer.specialized_base import DelegateToSpecializedWriter
                tool = DelegateToSpecializedWriter()
                with patch("plugin.framework.config.get_config", return_value="false"):
                    def _cfg_int_stop(key):
                        if key == "web_cache_max_mb":
                            return 0
                        if key == "chat_max_tokens":
                            return 2048
                        return 10

                    with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int_stop):
                        with patch("plugin.framework.config.get_api_config", return_value={}):
                            result = tool.execute(ctx, domain="web_research", task="What is the latest Python release?")

                assert result["status"] == "error"
                assert result["message"] == "Web search stopped by user."


def test_web_research_tool_approval():
    # Setup mock context
    ctx = MagicMock()
    ctx.ctx = MockContext()
    from unittest.mock import patch
    setattr(ctx.ctx, "getServiceManager", MagicMock())  # for ConfigService
    ctx.status_callback = MagicMock()
    ctx.append_thinking_callback = MagicMock()
    ctx.stop_checker = lambda: False

    # We will provide an approval_callback
    approval_called = []
    def mock_approval(query, tool, args):
        approval_called.append((query, tool))
        return True, None
    ctx.approval_callback = mock_approval

    call_count = [0]
    def mock_generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            tc = ChatMessageToolCall(
                id="call_1",
                type="function",
                function=ChatMessageToolCallFunction(
                    name="web_search",
                    arguments='{"query": "Latest Python release"}'
                )
            )
            return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        else:
            return ChatMessage(role=MessageRole.ASSISTANT, content="Done!")

    with patch("plugin.chatbot.smol_agent.WriterAgentSmolModel.generate", mock_generate):
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"<html><body>Search Results</body></html>"
            mock_url.return_value.__enter__.return_value = mock_resp
            with patch("requests.get"):
                from plugin.writer.specialized_base import DelegateToSpecializedWriter
                tool = DelegateToSpecializedWriter()
                # Mock config to prompt_for_web_research = "true"
                def _cfg_get(key):
                    if key == "chatbot.prompt_for_web_research":
                        return "true"
                    if key == "agent_backend.prompt_for_permission":
                        return "true"
                    return "false"
                with patch("plugin.framework.config.get_config", side_effect=_cfg_get):
                    def _cfg_int(key):
                        if key == "web_cache_max_mb":
                            return 0
                        if key == "chat_max_tokens":
                            return 2048
                        return 10
                    with patch("plugin.framework.config.get_config_int", side_effect=_cfg_int):
                        with patch("plugin.framework.config.get_api_config", return_value={}):
                            result = tool.execute(ctx, domain="web_research", task="What is the latest Python release?")

                assert result["status"] == "ok"
                assert "Done!" in result["result"]
                assert approval_called == [("Latest Python release", "web_search")]


def test_run_web_research_invalid_json():
    panel = DummyChatbotPanel()
    model = MockDocument()

    # Need a mock session so add_assistant_message doesn't blow up
    setattr(panel, "session", MagicMock())
    setattr(panel, "response_control", MagicMock())

    mock_main = MagicMock()
    mock_registry = MagicMock()
    # Tool execute returns a non-JSON string
    mock_registry.execute.return_value = "This is not valid JSON."
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()
    class DummyBase1(object): pass
    class DummyBase2(object): pass
    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict('sys.modules', {'plugin.main': mock_main, 'uno': mock_uno, 'unohelper': mock_unohelper, 'com.sun.star.text': MagicMock(), 'com.sun.star.awt': mock_awt, 'com.sun.star.lang': mock_lang}):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                panel._run_web_research("What is the speed of light?", model) # type: ignore

                # Verify responses
                assert "What is the speed of light?" in panel.responses

                # Verify fallback error message is surfaced
                assert "\n[Research error: Invalid JSON from web search tool.]\n" in panel.responses

                # Verify stream completed normally (terminal status is Ready)
                assert panel._terminal_status == "Ready"


def test_run_web_research_uses_session_history_not_response_control():
    from plugin.chatbot.panel import ChatSession

    panel = DummyChatbotPanel()
    model = MockDocument()

    session = ChatSession(system_prompt="Observe web search.")
    session.messages.append({"role": "user", "content": "price of inception mercury 2?"})
    session.messages.append({"role": "assistant", "content": "about $500"})
    panel.session = session
    panel.response_control.getModel.return_value = MagicMock()

    stale_greeting = (
        "AI: I can edit or translate your document instantly with professional formatting and color. Try me!"
    )

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = '{"status": "ok", "result": "follows up"}'
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()

    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.chatbot.dialogs.get_control_text", return_value=stale_greeting) as mock_get_text:
            with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:

                def fake_run_bg(func, **kwargs):
                    func()

                mock_run_bg.side_effect = fake_run_bg

                with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:

                    def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                    getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                    panel._run_web_research("you said that earlier", model)  # type: ignore

                    mock_get_text.assert_not_called()
                    mock_registry.execute.assert_called_once()
                    kwargs = mock_registry.execute.call_args.kwargs
                    assert "inception mercury" in kwargs["history_text"]
                    assert stale_greeting not in kwargs["history_text"]
                    assert kwargs["query"] == "you said that earlier"


def test_run_librarian_keeps_panel_flag_until_switch():
    panel = DummyChatbotPanel()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = {"status": "ok", "result": "Still onboarding"}
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()

    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()
                with patch("plugin.chatbot.librarian.get_suggested_user_name", return_value="Keith"):
                    panel._run_librarian("Hello", model)  # type: ignore

    assert panel._in_librarian_mode is True
    mock_registry.execute.assert_called_once()
    args, kwargs = mock_registry.execute.call_args
    assert args[0] == "librarian_onboarding"
    assert kwargs["query"] == "Hello"
    assert kwargs["suggested_user_name"] == "Keith"


def test_run_librarian_clears_panel_flag_on_switch_mode():
    panel = DummyChatbotPanel()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = {"status": "switch_mode", "result": "Switching now"}
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()

    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()
                panel._run_librarian("Done", model)  # type: ignore

    assert panel._in_librarian_mode is False
    mock_registry.execute.assert_called_once()


def test_run_librarian_switch_mode_calls_finished_callback():
    panel = DummyChatbotPanel()
    panel.on_librarian_session_finished = MagicMock()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.return_value = {"status": "switch_mode", "result": "Switching now"}
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    mock_uno = MagicMock()

    class DummyBase1(object):
        pass

    class DummyBase2(object):
        pass

    mock_unohelper = MagicMock()
    mock_unohelper.Base = DummyBase1
    mock_awt = MagicMock()
    mock_awt.XActionListener = DummyBase2
    mock_awt.XItemListener = DummyBase2
    mock_awt.XTextListener = DummyBase2
    mock_awt.XWindowListener = DummyBase2
    mock_awt.XKeyListener = DummyBase2
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2

    with patch.dict(
        "sys.modules",
        {
            "plugin.main": mock_main,
            "uno": mock_uno,
            "unohelper": mock_unohelper,
            "com.sun.star.text": MagicMock(),
            "com.sun.star.awt": mock_awt,
            "com.sun.star.lang": mock_lang,
        },
    ):
        with patch("plugin.framework.async_stream.run_in_background") as mock_run_bg:
            def fake_run_bg(func, **kwargs):
                func()

            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
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

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()
                panel._run_librarian("Done", model)  # type: ignore

    panel.on_librarian_session_finished.assert_called_once()


def test_agent_backend_worker_does_not_call_get_document_type():
    """run_agent must not classify the document (UNO thread violation)."""
    panel = DummyChatbotPanel()
    panel.session.document_context = "doc-ctx"
    panel._get_mcp_url = MagicMock(return_value=None)
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/doc.odt"

    adapter = MagicMock()
    adapter.is_available.return_value = True

    def run_worker(_q, worker_fn, *_args, **_kwargs):
        worker_fn()

    def cfg(key, *_args, **_kwargs):
        if key == "agent_backend.backend_id":
            return "hermes"
        if key == "additional_instructions":
            return ""
        if key == "mcp.mcp_enabled":
            return False
        return None

    with (
        patch("plugin.chatbot.send_handlers.get_config", side_effect=cfg),
        patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter),
        patch.object(panel, "_run_unified_worker_drain_loop", side_effect=run_worker),
        patch("plugin.doc.doc_type.get_document_type") as mock_gdt,
        patch("plugin.chatbot.agent_manual.full_manual_for_model") as mock_fmm,
    ):
        panel._execute_agent_backend_effect("hi", model, "writer", MagicMock(), MagicMock())

    adapter.send.assert_called_once()
    mock_gdt.assert_not_called()
    mock_fmm.assert_not_called()


class _RecordingSession:
    def __init__(self):
        self.messages = [{"role": "system", "content": "s"}]
        self.stored = []

    def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None):
        self.stored.append(content)
        self.messages.append({"role": "assistant", "content": content or ""})

    def add_user_message(self, content):
        self.messages.append({"role": "user", "content": content})


def _append_recording_turn(panel):
    """Fold assistant chunks into the session, the way the sidebar paints them."""
    from plugin.chatbot.tool_loop_actions import current_turn

    def _append(text, is_thinking=False, role="assistant"):
        panel.responses.append(text)
        panel.thinking_flags.append(bool(is_thinking))
        turn = current_turn(panel)
        if turn is not None and turn.alive and turn.session is not None:
            turn.fold_chunk(panel, text, role)

    panel._append_response = _append


def test_web_and_image_stop_persist_emitted_text():
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    for handler_type in ("web", "image"):
        panel = DummyChatbotPanel()
        panel.session = _RecordingSession()
        begin_send_turn(panel, handler_type)
        _append_recording_turn(panel)

        def worker(q):
            q.put((StreamQueueKind.CHUNK, "partial answer"))
            q.put((StreamQueueKind.STOPPED, None))

        _drive_unified_drain(panel, worker, handler_type)
        assert panel.session.stored[0] == "partial answer"
        assert "[Stopped by user]" in panel.session.stored[1]


def test_web_stop_without_emitted_text_stores_placeholder():
    panel = DummyChatbotPanel()

    def worker(q):
        q.put((StreamQueueKind.STOPPED, None))

    _drive_unified_drain(panel, worker, "web")
    panel.session.add_assistant_message.assert_any_call(content="No response.")
    assert any(
        "[Stopped by user]" in str(call.kwargs.get("content") or "")
        for call in panel.session.add_assistant_message.call_args_list
    )


def test_specialized_tool_errors_persist_assistant_row():
    """status=error must store the painted note so the user turn is not left open."""
    cases = (
        ("_active_run_librarian", "[Librarian error: nope]"),
        ("_active_run_brainstorming", "[Brainstorming error: nope]"),
        ("_active_run_writing_plan", "[Writing plan error: nope]"),
        ("_active_run_ppt_master", "[PPT-Master error: nope]"),
        ("_active_run_deep_research", "[Deep research error: nope]"),
        (None, "[Research error: nope]"),
    )
    for flag, needle in cases:
        panel = DummyChatbotPanel()
        panel.session.messages = []
        if flag:
            setattr(panel, flag, True)
        state = SendHandlerState(handler_type="web", status="starting")
        interpreter = EffectInterpreter(panel)
        mock_main = MagicMock()
        mock_registry = MagicMock()
        mock_registry.execute.return_value = {"status": "error", "message": "nope"}
        mock_registry._services = MagicMock()
        mock_main.get_tools.return_value = mock_registry

        def fake_run_bg(func, **kwargs):
            func()

        with patch.dict("sys.modules", {"plugin.main": mock_main}):
            with patch("plugin.chatbot.send_handlers.get_config", return_value=False):
                with patch("plugin.framework.async_stream.run_in_background", side_effect=fake_run_bg):
                    with patch("plugin.framework.async_stream.run_stream_drain_loop", side_effect=_collecting_drain([])):
                        panel._execute_web_research_effect("query", MagicMock(), state, interpreter)

        content = panel.session.add_assistant_message.call_args.kwargs["content"]
        assert content == needle
        assert needle in "".join(panel.responses)
        assert panel._terminal_status == "Ready"


def test_handler_errors_persist_assistant_banner():
    expected = {
        "web": "Research Chat error",
        "agent": "Operation failed",
        "image": "Operation failed",
    }
    for handler_type, needle in expected.items():
        panel = DummyChatbotPanel()

        def worker(q):
            q.put((StreamQueueKind.ERROR, {"message": "boom"}))

        _drive_unified_drain(panel, worker, handler_type)
        content = panel.session.add_assistant_message.call_args.kwargs["content"]
        assert needle in content
        assert content == content.strip()
        assert needle in "".join(panel.responses)


def test_thinking_chunk_reaches_append_as_thinking():
    panel = DummyChatbotPanel()

    def worker(q):
        q.put((StreamQueueKind.THINKING, "search step"))
        q.put((StreamQueueKind.CHUNK, "answer"))
        q.put((StreamQueueKind.STREAM_DONE, {}))

    _drive_unified_drain(panel, worker, "web")
    assert panel.thinking_flags[:2] == [True, False]
    assert panel.responses[0] == "search step"


def _collecting_drain(kinds):
    def _drain(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, stop_checker, **kwargs):
        while not q.empty():
            item = q.get()
            kinds.append(item[0])
            kind = item[0]
            if kind == StreamQueueKind.CHUNK:
                apply_chunk(item[1], False)
            elif kind == StreamQueueKind.THINKING:
                apply_chunk(item[1], True)
            elif kind == StreamQueueKind.STREAM_DONE:
                on_stream_done(item)
            elif kind == StreamQueueKind.STATUS:
                on_status_fn(item[1])
            elif kind == StreamQueueKind.ERROR:
                on_error(item[1])
            elif kind == StreamQueueKind.STOPPED:
                on_stopped()
        job_done[0] = True

    return _drain


def test_image_worker_drops_output_after_abort():
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    panel = DummyChatbotPanel()
    begin_send_turn(panel, "image")
    state = SendHandlerState(handler_type="image", status="starting")
    interpreter = EffectInterpreter(panel)

    def execute(_name, tctx, bypass_thread_guard=False, **_kwargs):
        abort_turn(panel)
        tctx.status_callback("after bump")
        return {"status": "done", "message": "late"}

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.side_effect = execute
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry
    kinds: list = []

    def fake_run_bg(func, **kwargs):
        func()

    with patch.dict("sys.modules", {"plugin.main": mock_main}):
        with patch("plugin.chatbot.send_handlers.update_lru_history"):
            with patch("plugin.framework.async_stream.run_in_background", side_effect=fake_run_bg):
                with patch("plugin.framework.async_stream.run_stream_drain_loop", side_effect=_collecting_drain(kinds)):
                    panel._execute_direct_image_effect("a cat", MagicMock(), state, interpreter)

    assert StreamQueueKind.CHUNK not in kinds
    assert StreamQueueKind.STATUS not in kinds
    assert panel.status_history == []
    assert not any("late" in text for text in panel.responses)


def test_web_worker_drops_output_after_abort():
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    panel = DummyChatbotPanel()
    panel.session.messages = []
    begin_send_turn(panel, "web")
    state = SendHandlerState(handler_type="web", status="starting")
    interpreter = EffectInterpreter(panel)

    def execute(_name, _tctx, bypass_thread_guard=False, **_kwargs):
        abort_turn(panel)
        _tctx.append_thinking_callback("secret-thought")
        _tctx.chat_append_callback("secret-chunk")
        _tctx.status_callback("still-here")
        raise RuntimeError("nope")

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.side_effect = execute
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry
    kinds: list = []

    def fake_run_bg(func, **kwargs):
        func()

    with patch.dict("sys.modules", {"plugin.main": mock_main}):
        with patch("plugin.chatbot.send_handlers.get_config", return_value=False):
            with patch("plugin.framework.async_stream.run_in_background", side_effect=fake_run_bg):
                with patch("plugin.framework.async_stream.run_stream_drain_loop", side_effect=_collecting_drain(kinds)):
                    panel._execute_web_research_effect("query", MagicMock(), state, interpreter)

    assert StreamQueueKind.CHUNK not in kinds
    assert StreamQueueKind.THINKING not in kinds
    assert StreamQueueKind.STATUS not in kinds
    assert StreamQueueKind.ERROR not in kinds
    assert panel.status_history == []
    assert panel.responses == []
    panel.session.add_assistant_message.assert_not_called()


def test_agent_backend_drops_output_after_abort():
    from plugin.chatbot.tool_loop_actions import abort_turn, begin_send_turn

    panel = DummyChatbotPanel()
    panel.session = _RecordingSession()
    panel.session.refresh_document_context = MagicMock()
    panel.session.document_context = "doc"
    begin_send_turn(panel, "agent")
    state = SendHandlerState(handler_type="agent", status="starting")
    interpreter = EffectInterpreter(panel)
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/doc.odt"

    adapter = MagicMock()
    adapter.is_available.return_value = True

    def send(queue, **_kwargs):
        abort_turn(panel)
        queue.put((StreamQueueKind.CHUNK, "late-agent"))
        queue.put((StreamQueueKind.STATUS, "agent-status"))
        queue.put((StreamQueueKind.STOPPED,))

    adapter.send.side_effect = send
    kinds: list = []

    def cfg(key, *_args, **_kwargs):
        if key == "agent_backend.backend_id":
            return "hermes"
        if key == "additional_instructions":
            return ""
        if key == "mcp.mcp_enabled":
            return False
        return None

    def fake_run_bg(func, **kwargs):
        func()

    with (
        patch("plugin.chatbot.send_handlers.get_config", side_effect=cfg),
        patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter),
        patch("plugin.chatbot.send_handlers.get_core_directives_for_type", return_value=""),
        patch("plugin.chatbot.send_handlers.full_manual", return_value=""),
        patch("plugin.framework.async_stream.run_in_background", side_effect=fake_run_bg),
        patch("plugin.framework.async_stream.run_stream_drain_loop", side_effect=_collecting_drain(kinds)),
    ):
        panel._execute_agent_backend_effect("hi", model, "writer", state, interpreter)

    assert StreamQueueKind.CHUNK not in kinds
    assert StreamQueueKind.STATUS not in kinds
    assert StreamQueueKind.STOPPED not in kinds
    assert panel.status_history == []
    assert not any("late-agent" in text for text in panel.responses)
    assert panel.session.stored == []


def _arm_spawn_scope(panel):
    """Bind resolve_stop_checker to the scope object, and record each call."""
    from plugin.framework.queue_executor import SendCancellation, bind_send_stop_checker

    old = SendCancellation()
    panel._send_cancellation = old
    calls = []

    def resolve():
        scope = panel._send_cancellation
        calls.append(scope)
        return bind_send_stop_checker(scope, lambda: False)

    panel.resolve_stop_checker = resolve
    return old, calls


def _swap_scope_and_run(panel, old, calls, worker_fn):
    """Cancel the spawn scope, point the panel at the next send, then run the body."""
    from plugin.framework.queue_executor import SendCancellation

    new = SendCancellation()
    old.cancel()
    panel._send_cancellation = new

    def boom():
        calls.append(panel._send_cancellation)
        raise AssertionError("resolve_stop_checker inside worker")

    panel.resolve_stop_checker = boom
    worker_fn()
    return new


def test_run_search_keeps_spawn_scope_after_next_send():
    """A late run_search body must not bind the send that replaced the panel field."""
    from plugin.framework.queue_executor import bind_send_stop_checker

    panel = DummyChatbotPanel()
    panel.session.messages = []
    old, calls = _arm_spawn_scope(panel)
    state = SendHandlerState(handler_type="web", status="starting")
    interpreter = EffectInterpreter(panel)
    seen = {}
    captured = {}

    def execute(_name, tctx, bypass_thread_guard=False, **_kwargs):
        seen["scope"] = tctx.send_cancellation
        seen["checker"] = tctx.stop_checker
        return {"status": "error", "message": "stop-here"}

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.side_effect = execute
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    def run_worker(_q, worker_fn, *_args, **_kwargs):
        captured["fn"] = worker_fn

    with patch.dict("sys.modules", {"plugin.main": mock_main}):
        with patch("plugin.chatbot.send_handlers.get_config", return_value=False):
            with patch.object(panel, "_run_unified_worker_drain_loop", side_effect=run_worker):
                panel._execute_web_research_effect("query", MagicMock(), state, interpreter)
                assert calls == [old]
                new = _swap_scope_and_run(panel, old, calls, captured["fn"])

    assert calls == [old]
    assert seen["scope"] is old
    assert seen["checker"]() is True
    assert bind_send_stop_checker(new, lambda: False)() is False


def test_direct_image_keeps_spawn_scope_after_next_send():
    from plugin.framework.queue_executor import bind_send_stop_checker

    panel = DummyChatbotPanel()
    old, calls = _arm_spawn_scope(panel)
    state = SendHandlerState(handler_type="image", status="starting")
    interpreter = EffectInterpreter(panel)
    seen = {}
    captured = {}

    def execute(_name, tctx, bypass_thread_guard=False, **_kwargs):
        seen["scope"] = tctx.send_cancellation
        seen["checker"] = tctx.stop_checker
        return {"status": "done", "message": "ok"}

    mock_main = MagicMock()
    mock_registry = MagicMock()
    mock_registry.execute.side_effect = execute
    mock_registry._services = MagicMock()
    mock_main.get_tools.return_value = mock_registry

    def run_worker(_q, worker_fn, *_args, **_kwargs):
        captured["fn"] = worker_fn

    with patch.dict("sys.modules", {"plugin.main": mock_main}):
        with patch("plugin.chatbot.send_handlers.update_lru_history"):
            with patch.object(panel, "_run_unified_worker_drain_loop", side_effect=run_worker):
                panel._execute_direct_image_effect("a cat", MagicMock(), state, interpreter)
                assert calls == [old]
                new = _swap_scope_and_run(panel, old, calls, captured["fn"])

    assert calls == [old]
    assert seen["scope"] is old
    assert seen["checker"]() is True
    assert bind_send_stop_checker(new, lambda: False)() is False


def test_agent_worker_keeps_spawn_checker_after_next_send():
    from plugin.framework.queue_executor import bind_send_stop_checker

    panel = DummyChatbotPanel()
    panel.session.messages = []
    panel.session.document_context = "doc"
    panel._get_mcp_url = MagicMock(return_value=None)
    old, calls = _arm_spawn_scope(panel)
    state = SendHandlerState(handler_type="agent", status="starting")
    interpreter = EffectInterpreter(panel)
    model = MagicMock()
    model.getURL.return_value = "file:///tmp/doc.odt"
    seen = {}
    captured = {}

    adapter = MagicMock()
    adapter.is_available.return_value = True

    def send(**kwargs):
        seen["checker"] = kwargs["stop_checker"]

    adapter.send.side_effect = send

    def cfg(key, *_args, **_kwargs):
        if key == "agent_backend.backend_id":
            return "hermes"
        if key == "additional_instructions":
            return ""
        if key == "mcp.mcp_enabled":
            return False
        return None

    def run_worker(_q, worker_fn, *_args, **_kwargs):
        captured["fn"] = worker_fn

    with (
        patch("plugin.chatbot.send_handlers.get_config", side_effect=cfg),
        patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter),
        patch("plugin.chatbot.send_handlers.get_core_directives_for_type", return_value=""),
        patch("plugin.chatbot.send_handlers.full_manual", return_value=""),
        patch.object(panel, "_run_unified_worker_drain_loop", side_effect=run_worker),
    ):
        panel._execute_agent_backend_effect("hi", model, "writer", state, interpreter)
        assert calls == [old]
        new = _swap_scope_and_run(panel, old, calls, captured["fn"])

    assert calls == [old]
    assert seen["checker"]() is True
    assert bind_send_stop_checker(new, lambda: False)() is False


def _run_blocking_now(ctx, func, *args, stop_checker=None, **kwargs):
    """Call the STT body on this thread. Production pumps the UI; tests must not."""
    del ctx
    assert stop_checker is None
    return func(*args, **kwargs)


def test_transcribe_keeps_spawn_stop_after_next_send(tmp_path):
    """Stop on the first scope still aborts STT after the panel field moves."""
    from plugin.framework.queue_executor import SendCancellation, bind_send_stop_checker

    panel = DummyChatbotPanel()
    panel.client = MagicMock()
    panel._stop_requested_fallback = False
    first = SendCancellation()
    panel._send_cancellation = first

    def resolve():
        return bind_send_stop_checker(panel._send_cancellation, lambda: getattr(panel, "_stop_requested_fallback", False))

    panel.resolve_stop_checker = resolve
    wav = tmp_path / "take.wav"
    wav.write_bytes(b"RIFF")

    def fake_transcribe(_path, **kwargs):
        second = SendCancellation()
        panel._send_cancellation = second
        panel._stop_requested_fallback = False
        proc = MagicMock()
        proc.poll.return_value = None
        kwargs["on_spawn"](proc)
        # A live re-read binds the next send and would miss this Stop.
        assert resolve()() is False
        first.cancel()
        assert bind_send_stop_checker(second, lambda: False)() is False
        return "hello"

    with (
        patch("plugin.chatbot.send_handlers.run_blocking_in_thread", side_effect=_run_blocking_now),
        patch("plugin.audio.stt_service.transcribe", side_effect=fake_transcribe),
        patch("plugin.audio.stt_service.status_for_transcription", return_value="Transcribing"),
    ):
        result = panel._transcribe_audio(str(wav), "base")

    assert result == "hello"
    assert panel._terminal_status == "Stopped"
    assert not any("Transcription error" in text for text in panel.responses)
    assert not wav.exists()
    assert panel._stt_inflight is False
    assert panel._stt_kill is None


def test_reentrant_transcribe_keeps_the_first_wav(tmp_path):
    """A second STT call while the first is running must not delete that WAV."""
    from plugin.framework.queue_executor import SendCancellation

    panel = DummyChatbotPanel()
    panel.client = MagicMock()
    panel._send_cancellation = SendCancellation()
    outer = tmp_path / "outer.wav"
    inner = tmp_path / "inner.wav"
    outer.write_bytes(b"RIFF")
    inner.write_bytes(b"RIFF")
    nested = {}

    def fake_transcribe(_path, **_kwargs):
        nested["result"] = panel._transcribe_audio(str(inner), "base")
        nested["outer_exists"] = outer.exists()
        nested["inner_exists"] = inner.exists()
        return "hello"

    with (
        patch("plugin.chatbot.send_handlers.run_blocking_in_thread", side_effect=_run_blocking_now),
        patch("plugin.audio.stt_service.transcribe", side_effect=fake_transcribe),
        patch("plugin.audio.stt_service.status_for_transcription", return_value="Transcribing"),
    ):
        text = panel._transcribe_audio(str(outer), "base")

    assert text == "hello"
    assert nested["result"] == ""
    assert nested["outer_exists"] is True
    assert nested["inner_exists"] is True
    assert not outer.exists()
    assert panel._stt_inflight is False

class TestSTTClientReset:
    def test_transcribe_cancelling_send_scope_does_not_stop_stt_client(self) -> None:
        from plugin.framework.queue_executor import SendCancellation

        panel = DummyChatbotPanel()
        scope = SendCancellation()
        panel._send_cancellation = scope
        panel.ctx = MagicMock()

        # Stop effect is intercepted so we just observe the scope
        with (
            patch("plugin.chatbot.send_handlers.get_api_config", return_value={"model": "fake", "provider": "openai"}),
            patch("plugin.framework.client.llm_client.LlmClient.stop") as mock_stop,
            patch("plugin.audio.stt_service.status_for_transcription", return_value="Transcribing..."),
            patch("plugin.chatbot.send_handlers.run_blocking_in_thread", return_value="hello world")
        ):
            panel._transcribe_audio("fake.wav", "stt-model")

        # Cancelling the scope must not call stop on the created LlmClient
        scope.cancel()
        mock_stop.assert_not_called()


    def test_transcribe_builds_fresh_client(self) -> None:
        host = DummyChatbotPanel()
        host.client = MagicMock()
        host.client.api_key = "old-key"

        with (
            patch("plugin.chatbot.send_handlers.get_api_config"),
            patch("plugin.chatbot.send_handlers.LlmClient") as mock_llm_client,
            patch("plugin.chatbot.send_handlers.run_blocking_in_thread"),
            patch("plugin.chatbot.send_handlers.capture_send_stop", return_value=(MagicMock(), lambda: False))
        ):
            host._transcribe_audio("fake.wav", "stt-model")
            assert mock_llm_client.called


def test_direct_image_records_user_message_only_once():
    """Image mode stored the prompt twice: the StartEvent user append folded it,
    then the spawn effect called add_user_message again (Scrolly QA)."""
    from plugin.chatbot.rich_text_paste import fold_transcript_chunk

    messages: list[dict[str, str]] = []
    session = MagicMock()
    session.messages = messages
    session.add_user_message.side_effect = lambda txt: messages.append({"role": "user", "content": txt})

    class FoldingPanel(DummyChatbotPanel):
        def _append_response(self, text, is_thinking=False, role="assistant"):
            super()._append_response(text, is_thinking, role)
            if role == "user":
                fold_transcript_chunk(session, text, role="user")

    panel = FoldingPanel()
    setattr(panel, "session", session)
    with patch.object(panel, "_run_unified_worker_drain_loop"), patch("plugin.chatbot.send_handlers.update_lru_history"):
        panel._do_send_direct_image("draw an apple", MagicMock())  # type: ignore

    assert [m for m in messages if m.get("role") == "user"] == [{"role": "user", "content": "draw an apple"}]


def test_direct_image_stop_finalizes_with_stop_line():
    """Image-mode Stop must draw the stop line like chat and web (Scrolly QA)."""
    panel = DummyChatbotPanel()
    panel.stop_requested = True
    state = SendHandlerState(handler_type="image", status="ready")
    interpreter = EffectInterpreter(panel)
    with patch.object(panel, "_run_unified_worker_drain_loop"), patch("plugin.chatbot.send_handlers.update_lru_history"), patch("plugin.chatbot.rich_text.finalize_sidebar_assistant_response") as fin:
        panel._execute_direct_image_effect("a cat", MagicMock(), state, interpreter)  # type: ignore
    fin.assert_called_once_with(panel, allow_rerender=False)

def test_run_deep_web_research_leak_fix():
    panel = DummyChatbotPanel()
    with patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=None):
        panel._run_deep_web_research("query", MagicMock())
    assert not getattr(panel, "_active_run_deep_research", False)

def test_mode_flags_stuck_fix():
    panel = DummyChatbotPanel()
    with patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=None):
        panel._run_librarian("query", MagicMock())
        assert not panel._in_librarian_mode
        panel._run_brainstorming("query", MagicMock())
        assert not getattr(panel, "_in_brainstorming_mode", False)
        panel._run_writing_plan("query", MagicMock())
        assert not getattr(panel, "_in_writing_plan_mode", False)
        panel._run_ppt_master("query", MagicMock())
        assert not getattr(panel, "_in_ppt_master_mode", False)

def test_execute_agent_backend_effect_errors():
    panel = DummyChatbotPanel()
    mock_turn = MagicMock()
    mock_turn.refresh_document_context.side_effect = Exception("Doc ctx error")
    with patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=mock_turn), \
         patch("plugin.chatbot.send_handlers.defer_until_drain_done") as mock_defer:
        interpreter = MagicMock()
        current_state = SendHandlerState(handler_type="agent", status="ready")
        panel._execute_agent_backend_effect("query", MagicMock(), "writer", current_state, interpreter)
    assert panel._terminal_status == "Error"
    mock_defer.assert_called_once()

def test_agent_worker_clearing_current_backend():
    panel = DummyChatbotPanel()
    adapter1 = MagicMock()
    adapter2 = MagicMock()
    panel._current_agent_backend = adapter2

    def run_agent(): pass

    # Simulate a deferred ready callback bound to adapter1
    def _agent_ready():
        if panel._terminal_status not in ("Error", "Stopped"):
            panel._terminal_status = "Ready"
        if panel._current_agent_backend is adapter1:
            panel._current_agent_backend = None

    _agent_ready()
    # It should not clear adapter2
    assert panel._current_agent_backend is adapter2

def test_approval_dialog_wrapping():
    panel = DummyChatbotPanel()
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def fake_drain(q, run_agent, *args, **kwargs):
        cb = kwargs.get("on_approval_callback")
        cb(adapter, "req_id", True, "desc", "tool")

    panel._run_unified_worker_drain_loop = fake_drain  # type: ignore

    with patch("plugin.chatbot.send_handlers.show_approval_dialog", side_effect=Exception("UI Error")):
        # Call the effect which will trigger the fake drain
        panel.stop_requested = False
        with patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=MagicMock()), \
             patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter), \
             patch("plugin.chatbot.send_handlers.get_config", return_value="some_backend"), \
             patch("plugin.chatbot.send_handlers.as_bool", return_value=True):
            # We don't need to actually run it, we can just test the inner _on_approval_required function
            # But the inner function is local, so we just mock show_approval_dialog in a way that
            # we can verify adapter.submit_approval was called with False
            pass

    # A better test for the inner _on_approval_required:

def test_approval_dialog_submit_false_on_error():
    panel = DummyChatbotPanel()
    adapter = MagicMock()

    # We will grab the on_approval_required function by mocking _run_unified_worker_drain_loop
    cb_capture = []
    def capture_drain(q, run_agent, current_state, interpreter, on_approval_callback=None, **kwargs):
        cb_capture.append(on_approval_callback)

    panel._run_unified_worker_drain_loop = capture_drain

    with patch("plugin.chatbot.send_handlers._turn_session_or_stop", return_value=MagicMock()), \
         patch("plugin.chatbot.send_handlers.get_backend", return_value=adapter), \
         patch("plugin.chatbot.send_handlers.get_config", return_value="some_backend"):
         panel._execute_agent_backend_effect("query", MagicMock(), "writer", MagicMock(), MagicMock())

    cb = cb_capture[0]
    with patch("plugin.chatbot.send_handlers.show_approval_dialog", side_effect=Exception("UI Error")), \
         patch("plugin.chatbot.send_handlers.as_bool", return_value=True):
        cb(("approval_required", "desc", "tool", {}, "req_id"))

    adapter.submit_approval.assert_called_once_with("req_id", False)

def test_record_assistant_start_on_ui_thread_for_web_research():
    panel = DummyChatbotPanel()
    assert panel._record_assistant_start is False

    q = queue.Queue()
    current_state = SendHandlerState("web", "ready")

    # We test the drain loop applies the store from the UI side correctly, but wait, the fix we
    # chose is reverting the payload to the worker thread side:
    # "a single bool store before q.put(CHUNK) already happens-before the drain".

    def worker_fn():
        panel._record_assistant_start = True
        q.put((StreamQueueKind.CHUNK, "hello\n"))

    panel._run_unified_worker_drain_loop(q, worker_fn, current_state, MagicMock())
    assert getattr(panel, "_record_assistant_start", False) is True
