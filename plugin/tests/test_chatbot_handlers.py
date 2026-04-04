from unittest.mock import MagicMock, patch

from plugin.framework.async_stream import StreamQueueKind
from plugin.modules.chatbot.send_handlers import SendHandlersMixin
from plugin.modules.chatbot.web_research import WebResearchTool
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
        self.status_history = []
        self._terminal_status = None
        setattr(self, "session", MagicMock())
        setattr(self, "response_control", MagicMock())

        # UI Mocks
        self.aspect_ratio_selector = MagicMock()
        self.aspect_ratio_selector.getText.return_value = "Landscape (16:9)"

        self.image_model_selector = MagicMock()
        self.image_model_selector.getText.return_value = "dall-e-3"

        self.base_size_input = MagicMock()
        self.base_size_input.getText.return_value = "1024"

    def _append_response(self, text):
        self.responses.append(text)

    def _set_status(self, text):
        self.status_history.append(text)

    # We need to mock _get_doc_type_str since SendHandlersMixin uses it implicitly in some places
    def _get_doc_type_str(self, model):
        return "Writer"


def test_do_send_direct_image():
    panel = DummyChatbotPanel()
    model = MockDocument()

    # We need to mock plugin.main explicitly to avoid import errors due to missing LibreOffice UNO dependencies
    # during testing.
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
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict('sys.modules', {'plugin.main': mock_main, 'uno': mock_uno, 'unohelper': mock_unohelper, 'com.sun.star.text': MagicMock(), 'com.sun.star.awt': mock_awt, 'com.sun.star.lang': mock_lang}):
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_bg:
            # Synchronous execution of background worker
            def fake_run_bg(func):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                # Trigger stream done immediately
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, ctx, stop_checker, **kwargs):
                    while not q.empty():
                        item = q.get()
                        k = item[0]
                        if k == StreamQueueKind.CHUNK:
                            apply_chunk(item[1])
                        elif k == StreamQueueKind.STREAM_DONE:
                            on_stream_done(item[1])
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])
                mock_run_stream.side_effect = fake_drain_loop

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                from typing import cast
                from plugin.modules.chatbot.send_handlers import SendHandlerHost
                # We tell the type checker to treat panel as a SendHandlerHost to bypass the static error.
                # In Python < 3.12 `cast(SendHandlerHost, panel)` could work, but using type ignores
                # is simpler and less prone to edge-cases with Protocol types in ty/mypy.
                panel._do_send_direct_image("A cute dog", model)  # type: ignore

                # Verify responses
                assert "\nYou: A cute dog\n" in panel.responses
                assert "AI: Creating image...\n" in panel.responses
                assert any("generate_image: Image generated successfully" in r for r in panel.responses)

                # Verify tool registry was called
                mock_registry.execute.assert_called_once()
                args, kwargs = mock_registry.execute.call_args
                assert args[0] == "generate_image"
                assert kwargs["prompt"] == "A cute dog"
                assert kwargs["aspect_ratio"] == "landscape_16_9"
                assert kwargs["base_size"] == 1024
                assert kwargs["image_model"] == "dall-e-3"

def test_do_send_direct_image_error():
    panel = DummyChatbotPanel()
    model = MockDocument()

    mock_main = MagicMock()
    mock_registry = MagicMock()
    # Tool execute returns an error payload
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
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict('sys.modules', {'plugin.main': mock_main, 'uno': mock_uno, 'unohelper': mock_unohelper, 'com.sun.star.text': MagicMock(), 'com.sun.star.awt': mock_awt, 'com.sun.star.lang': mock_lang}):
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_bg:
            # Synchronous execution of background worker
            def fake_run_bg(func):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                # Trigger stream done immediately
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, ctx, stop_checker, **kwargs):
                    while not q.empty():
                        item = q.get()
                        k = item[0]
                        if k == StreamQueueKind.CHUNK:
                            apply_chunk(item[1])
                        elif k == StreamQueueKind.STREAM_DONE:
                            on_stream_done(item[1])
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])
                mock_run_stream.side_effect = fake_drain_loop

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                panel._do_send_direct_image("A cute dog", model) # type: ignore

                # Verify responses
                assert "\nYou: A cute dog\n" in panel.responses
                assert "AI: Creating image...\n" in panel.responses

                # Verify error message is surfaced to user
                assert "[generate_image: Failed to generate image]\n" in panel.responses

                # Verify stream completed normally (terminal status is Ready)
                assert panel._terminal_status == "Ready"

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
            # Look at messages to see what happened
            # The last message should be the tool response
            last_msg = messages[-1]
            assert last_msg.role == MessageRole.TOOL_RESPONSE

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


    with patch("plugin.framework.smol_model.WriterAgentSmolModel.generate", new=mock_generate):
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

                from plugin.modules.writer.specialized import DelegateToSpecializedWriter
                tool = DelegateToSpecializedWriter()
                with patch("plugin.framework.config.get_config", return_value="false"):
                    with patch("plugin.framework.config.get_config_int", return_value=10):
                        with patch("plugin.framework.config.get_api_config", return_value={"chat_max_tokens": 2048}):
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

    with patch("plugin.framework.smol_model.WriterAgentSmolModel.generate", return_value=ChatMessage(role=MessageRole.ASSISTANT, content="")):
        with patch("urllib.request.urlopen"):
            with patch("requests.get"):
                from plugin.modules.writer.specialized import DelegateToSpecializedWriter
                tool = DelegateToSpecializedWriter()
                with patch("plugin.framework.config.get_config", return_value="false"):
                    with patch("plugin.framework.config.get_config_int", return_value=10):
                        with patch("plugin.framework.config.get_api_config", return_value={"chat_max_tokens": 2048}):
                            result = tool.execute(ctx, domain="web_research", task="What is the latest Python release?")

                assert result["status"] == "error"
                assert result["message"] == "Web search stopped by user."


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
    mock_lang = MagicMock()
    mock_lang.XEventListener = DummyBase2
    with patch.dict('sys.modules', {'plugin.main': mock_main, 'uno': mock_uno, 'unohelper': mock_unohelper, 'com.sun.star.text': MagicMock(), 'com.sun.star.awt': mock_awt, 'com.sun.star.lang': mock_lang}):
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_bg:
            # Synchronous execution of background worker
            def fake_run_bg(func):
                func()
            mock_run_bg.side_effect = fake_run_bg

            with patch("plugin.framework.async_stream.run_stream_drain_loop") as mock_run_stream:
                # Trigger stream done immediately
                def fake_drain_loop(q, toolkit, job_done, apply_chunk, on_stream_done, on_stopped, on_error, on_status_fn, ctx, stop_checker, **kwargs):
                    while not q.empty():
                        item = q.get()
                        k = item[0]
                        if k == StreamQueueKind.CHUNK:
                            apply_chunk(item[1])
                        elif k == StreamQueueKind.STREAM_DONE:
                            on_stream_done(item[1])
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])
                mock_run_stream.side_effect = fake_drain_loop

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()

                panel._run_web_research("What is the speed of light?", model) # type: ignore

                # Verify responses
                assert "\nYou: What is the speed of light?\n" in panel.responses

                # Verify fallback error message is surfaced
                assert "\n[Research error: Invalid JSON from web search tool.]\n" in panel.responses

                # Verify stream completed normally (terminal status is Ready)
                assert panel._terminal_status == "Ready"


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
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_bg:
            def fake_run_bg(func):
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
                            on_stream_done(item[1])
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])

                mock_run_stream.side_effect = fake_drain_loop

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()
                panel._run_librarian("Hello", model)  # type: ignore

    assert panel._in_librarian_mode is True
    mock_registry.execute.assert_called_once()
    args, kwargs = mock_registry.execute.call_args
    assert args[0] == "librarian_onboarding"
    assert kwargs["query"] == "Hello"


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
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_bg:
            def fake_run_bg(func):
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
                            on_stream_done(item[1])
                        elif k == StreamQueueKind.STATUS:
                            on_status_fn(item[1])
                        elif k == StreamQueueKind.ERROR:
                            on_error(item[1])

                mock_run_stream.side_effect = fake_drain_loop

                getattr(panel.ctx, "getServiceManager")().createInstanceWithContext.return_value = MagicMock()
                panel._run_librarian("Done", model)  # type: ignore

    assert panel._in_librarian_mode is False
    mock_registry.execute.assert_called_once()
