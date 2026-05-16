import unittest
import os
import sys
import io
from unittest.mock import patch

# Add plugin to path if needed
if os.getcwd() not in sys.path:
    sys.path.append(os.getcwd())

# Mock env var BEFORE importing harness
os.environ["WRITERAGENT_SHOW_CONSOLE"] = "1"

import plugin.scripting.worker_harness as worker_harness

class TestWorkerHarnessConsole(unittest.TestCase):
    def test_tee(self):
        buf1 = io.StringIO()
        buf2 = io.StringIO()
        tee = worker_harness.Tee(buf1, buf2)
        tee.write("hello")
        self.assertEqual(buf1.getvalue(), "hello")
        self.assertEqual(buf2.getvalue(), "hello")

    @patch('sys.__stdout__', new_callable=io.StringIO)
    @patch('sys.__stdin__', new_callable=io.StringIO)
    def test_execute_request_console(self, mock_stdin, mock_stdout):
        mock_stdin.write("user input\n")
        mock_stdin.seek(0)
        
        # Save original stdin to check restoration
        orig_stdin = sys.stdin
        
        code = "print('output to console')\nresult = input()"
        resp = worker_harness._execute_request(code, None)
        
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["result"], "user input")
        self.assertIn("output to console", resp["stdout"])
        self.assertIn("output to console", mock_stdout.getvalue())
        
        # Check stdin restoration
        self.assertIs(sys.stdin, orig_stdin)

if __name__ == '__main__':
    unittest.main()
