import os
import sys
import unittest
import queue

# Add project root to sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from unittest.mock import MagicMock
from plugin.modules.core.async_stream import run_stream_drain_loop

class TestAsyncStream(unittest.TestCase):
    def test_drain_loop_basic(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        chunks = []

        def apply_chunk(text, is_thinking=False):
            chunks.append((text, is_thinking))

        def on_stream_done(data):
            return True

        # Put some items in the queue
        q.put(("thinking", "I am thinking"))
        q.put(("chunk", "Hello world"))
        q.put(("stream_done", {"status": "ok"}))

        # Run the loop
        run_stream_drain_loop(
            q, toolkit, job_done, apply_chunk, 
            on_stream_done, MagicMock(), MagicMock()
        )

        # Verify thinking block was wrapped correctly
        self.assertEqual(chunks[0], ("[Thinking] ", True))
        self.assertEqual(chunks[1], ("I am thinking", True))
        # Content chunk should close thinking
        self.assertEqual(chunks[2], (" /thinking\n", True))
        self.assertEqual(chunks[3], ("Hello world", False))
        self.assertTrue(job_done[0])

    def test_drain_loop_error(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        errors = []

        def on_error(e):
            errors.append(e)

        q.put(("error", "Something went wrong"))

        run_stream_drain_loop(
            q, toolkit, job_done, MagicMock(), 
            MagicMock(), MagicMock(), on_error
        )

        self.assertEqual(errors[0], "Something went wrong")
        self.assertTrue(job_done[0])

    def test_drain_loop_stopped(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        stopped_called = [False]

        def on_stopped():
            stopped_called[0] = True

        q.put(("stopped",))

        run_stream_drain_loop(
            q, toolkit, job_done, MagicMock(), 
            MagicMock(), on_stopped, MagicMock()
        )

        self.assertTrue(stopped_called[0])
        self.assertTrue(job_done[0])

if __name__ == "__main__":
    unittest.main()
