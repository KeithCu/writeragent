import queue
import unittest
from unittest.mock import Mock, patch

from plugin.framework.errors import WriterAgentException, format_error_payload
from plugin.framework.tool_base import ToolBase
from plugin.modules.http.client import format_error_for_display

class DummyTool(ToolBase):
    name = "dummy_tool"
    description = "Dummy Tool"

    def execute(self, **kwargs):
        pass

class TestErrorHandling(unittest.TestCase):
    def test_format_error_payload_writer_agent_exception(self):
        exc = WriterAgentException("Test message", code="TEST_CODE", details={"key": "value"})
        payload = format_error_payload(exc)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "TEST_CODE")
        self.assertEqual(payload["message"], "Test message")
        self.assertEqual(payload["details"], {"key": "value"})

    def test_format_error_payload_generic_exception(self):
        exc = ValueError("Test message")
        payload = format_error_payload(exc)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "INTERNAL_ERROR")
        self.assertEqual(payload["message"], "Test message")
        self.assertEqual(payload["details"], {"type": "ValueError"})

    def test_tool_base_error_formatting(self):
        tool = DummyTool()
        result = tool._tool_error("Tool failed", code="CUSTOM_CODE", key="val")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "CUSTOM_CODE")
        self.assertEqual(result["message"], "Tool failed")
        self.assertEqual(result["details"], {"key": "val"})

    def test_format_error_for_display(self):
        exc = WriterAgentException("User error", code="ERR")
        display_str = format_error_for_display(exc)
        self.assertEqual(display_str, "Error: User error")

        exc_generic = ValueError("System error")
        display_str_generic = format_error_for_display(exc_generic)
        self.assertEqual(display_str_generic, "Error: System error")

from plugin.framework.async_stream import run_stream_drain_loop

class TestAsyncStreamErrorHandling(unittest.TestCase):
    def test_run_stream_drain_loop_error_handler(self):
        q = queue.Queue()
        job_done = [False]

        # Simulate a worker thread putting a formatted error
        test_error = ValueError("Simulation error")
        formatted_error = format_error_payload(test_error)
        q.put(("error", formatted_error))

        error_received = []

        def on_error(e):
            error_received.append(e)

        class DummyToolkit:
            def processEventsToIdle(self):
                pass

        run_stream_drain_loop(
            q, DummyToolkit(), job_done,
            lambda c, t: None,
            on_error=on_error,
            # Pass functions that just return True or None to satisfy signatures
            on_stream_done=lambda x: True,
            on_stopped=lambda: None
        )

        self.assertTrue(job_done[0])
        self.assertEqual(len(error_received), 1)
        self.assertEqual(error_received[0]["status"], "error")
        self.assertEqual(error_received[0]["message"], "Simulation error")

if __name__ == "__main__":
    unittest.main()
