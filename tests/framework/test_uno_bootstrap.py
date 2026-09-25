import pytest
import io
import sys

from plugin.framework import uno_bootstrap


class TestEnsureUtf8Stdio:
    def setup_method(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def teardown_method(self) -> None:
        uno_bootstrap._stdio_utf8_done = False

    def test_reconfigure_ascii_stream_allows_unicode_print(self) -> None:
        buffer = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = ascii_stdout
            with pytest.raises(UnicodeEncodeError):
                print("hello \U0001f44b \u2014")
            buffer.seek(0)
            buffer.truncate(0)

            uno_bootstrap.ensure_utf8_stdio()
            assert (sys.stdout.encoding) == ("utf-8")
            print("hello \U0001f44b \u2014")
            sys.stdout.flush()
            assert (b"hello") in (buffer.getvalue())
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
            assert (sys.stdout.encoding) == (first_encoding)
        finally:
            sys.stdout = old_stdout


class TestEnsurePluginOnPathDoc:
    def test_ensure_plugin_on_path_has_docstring(self) -> None:
        assert (uno_bootstrap.ensure_plugin_on_path.__doc__)


