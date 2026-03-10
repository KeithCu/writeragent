# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import os
import sys
import unittest
import queue

# Add project root to sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from unittest.mock import MagicMock
from plugin.framework.async_stream import run_stream_drain_loop

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

    def test_drain_loop_misc_events(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        status_called = []
        approval_called = []

        def on_status(data):
            status_called.append(data)

        def on_approval(item):
            approval_called.append(item)

        def on_stream_done(item):
            return True

        q.put(("status", "Processing"))
        q.put(("tool_thinking", "Searching..."))
        q.put(("approval_required", "Require user approval"))
        q.put(("tool_done", {"result": "ok"}))

        chunks = []
        def apply_chunk(text, is_thinking=False):
            chunks.append((text, is_thinking))

        run_stream_drain_loop(
            q, toolkit, job_done, apply_chunk,
            on_stream_done, MagicMock(), MagicMock(),
            on_status_fn=on_status, show_search_thinking=True,
            on_approval_required=on_approval
        )

        self.assertEqual(status_called, ["Processing"])
        self.assertEqual(chunks, [("Searching...", True)])
        self.assertEqual(approval_called, [("approval_required", "Require user approval")])
        self.assertTrue(job_done[0])

    def test_run_blocking_in_thread(self):
        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr

        def blocking_func():
            return "success"

        from plugin.framework.async_stream import run_blocking_in_thread
        res = run_blocking_in_thread(ctx, blocking_func)
        self.assertEqual(res, "success")

    def test_run_blocking_in_thread_error(self):
        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr

        def blocking_func():
            raise ValueError("failed")

        from plugin.framework.async_stream import run_blocking_in_thread
        with self.assertRaises(ValueError):
            run_blocking_in_thread(ctx, blocking_func)

    def test_run_stream_completion_async(self):
        ctx = MagicMock()
        client = MagicMock()

        def mock_stream_completion(prompt, sys_prompt, max_tokens, append_callback, append_thinking_callback, status_callback, stop_checker):
            append_callback("hello ")
            append_thinking_callback("hmm")
            status_callback("running")

        client.stream_completion.side_effect = mock_stream_completion

        apply_called = []
        def apply_chunk(text, is_thinking):
            apply_called.append((text, is_thinking))

        done_called = [False]
        def on_done():
            done_called[0] = True

        from plugin.framework.async_stream import run_stream_completion_async
        run_stream_completion_async(
            ctx, client, "p", "s", 100, apply_chunk, on_done, MagicMock(), MagicMock()
        )

        self.assertTrue(done_called[0])
        self.assertIn(("hello ", False), apply_called)
        self.assertIn(("[Thinking] ", True), apply_called)
        self.assertIn(("hmm", True), apply_called)

    def test_run_stream_async(self):
        ctx = MagicMock()
        client = MagicMock()

        def mock_stream_chat(messages, max_tokens, append_callback, append_thinking_callback, stop_checker):
            append_callback("hello")

        client.stream_chat_response.side_effect = mock_stream_chat

        apply_called = []
        def apply_chunk(text, is_thinking):
            apply_called.append((text, is_thinking))

        done_called = [False]
        def on_done():
            done_called[0] = True

        from plugin.framework.async_stream import run_stream_async
        run_stream_async(
            ctx, client, [{"role": "user", "content": "p"}], tools=None,
            apply_chunk_fn=apply_chunk, on_done_fn=on_done, on_error_fn=MagicMock()
        )

        self.assertTrue(done_called[0])
        self.assertIn(("hello", False), apply_called)

    def test_run_stream_async_with_tools(self):
        ctx = MagicMock()
        client = MagicMock()

        def mock_stream_chat(messages, max_tokens, tools, append_callback, append_thinking_callback, stop_checker):
            append_callback("tool call")

        client.stream_request_with_tools.side_effect = mock_stream_chat

        apply_called = []
        def apply_chunk(text, is_thinking):
            apply_called.append((text, is_thinking))

        done_called = [False]
        def on_done():
            done_called[0] = True

        from plugin.framework.async_stream import run_stream_async
        run_stream_async(
            ctx, client, [{"role": "user", "content": "p"}], tools=[{"type": "function"}],
            apply_chunk_fn=apply_chunk, on_done_fn=on_done, on_error_fn=MagicMock()
        )

        self.assertTrue(done_called[0])
        self.assertIn(("tool call", False), apply_called)

    def test_run_stream_async_stop_checker(self):
        ctx = MagicMock()
        client = MagicMock()

        def mock_stream_chat(messages, max_tokens, append_callback, append_thinking_callback, stop_checker):
            append_callback("hello")

        client.stream_chat_response.side_effect = mock_stream_chat

        done_called = [False]
        def on_done():
            done_called[0] = True

        from plugin.framework.async_stream import run_stream_async
        run_stream_async(
            ctx, client, [{"role": "user", "content": "p"}], tools=None,
            apply_chunk_fn=MagicMock(), on_done_fn=on_done, on_error_fn=MagicMock(), stop_checker=lambda: True
        )

        self.assertTrue(done_called[0])

    def test_drain_loop_final_done(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]

        def on_stream_done(item):
            return True

        q.put(("final_done", {"result": "ok"}))

        run_stream_drain_loop(
            q, toolkit, job_done, MagicMock(),
            on_stream_done, MagicMock(), MagicMock(),
        )
        self.assertTrue(job_done[0])

    def test_drain_loop_next_tool(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]

        def on_stream_done(item):
            return True

        q.put(("next_tool", {"result": "ok"}))

        run_stream_drain_loop(
            q, toolkit, job_done, MagicMock(),
            on_stream_done, MagicMock(), MagicMock(),
        )
        self.assertTrue(job_done[0])

if __name__ == "__main__":
    unittest.main()
