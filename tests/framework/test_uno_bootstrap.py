import io
import sys
import unittest

from plugin.framework import uno_bootstrap


class TestEnsureUtf8Stdio(unittest.TestCase):
    def setUp(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def tearDown(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def test_reconfigure_ascii_stream_allows_unicode_print(self) -> None:
        buffer = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = ascii_stdout
            with self.assertRaises(UnicodeEncodeError):
                print("hello \U0001f44b \u2014")
            buffer.seek(0)
            buffer.truncate(0)

            uno_bootstrap.ensure_utf8_stdio()
            self.assertEqual(sys.stdout.encoding, "utf-8")
            print("hello \U0001f44b \u2014")
            sys.stdout.flush()
            self.assertIn(b"hello", buffer.getvalue())
        finally:
            sys.stdout = old_stdout

    def test_ensure_utf8_stdio_runs_once(self) -> None:
        buffer = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = ascii_stdout
            uno_bootstrap.ensure_utf8_stdio()
            first_encoding = sys.stdout.encoding
            uno_bootstrap.ensure_utf8_stdio()
            self.assertEqual(sys.stdout.encoding, first_encoding)
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
