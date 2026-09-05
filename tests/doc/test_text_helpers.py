from unittest.mock import MagicMock, patch

from plugin.doc.text_helpers import (
    get_document_path,
    get_full_writer_text,
    get_string_without_tracked_deletions,
    normalize_file_url,
    normalize_linebreaks,
)


def test_normalize_linebreaks():
    assert normalize_linebreaks("hello\r\nworld") == "hello\nworld"
    assert normalize_linebreaks("hello\n\rworld") == "hello\nworld"
    assert normalize_linebreaks("hello\rworld") == "hello\nworld"
    assert normalize_linebreaks("Line 1\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\r\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\rLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\n\rLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("A\r\nB\rC\n\rD\nE") == "A\nB\nC\nD\nE"
    assert normalize_linebreaks("\r\n\r\n") == "\n\n"
    assert normalize_linebreaks("\n\r\n\r") == "\n\n"
    assert normalize_linebreaks("\r\r") == "\n\n"
    assert normalize_linebreaks("") == ""
    assert normalize_linebreaks(None) == ""


def test_normalize_file_url_repairs_legacy_urljoin_form():
    assert normalize_file_url("file:/home/user/Writing/Test.odt") == "file:///home/user/Writing/Test.odt"
    assert normalize_file_url("file:///home/user/Writing/Test.odt") == "file:///home/user/Writing/Test.odt"
    assert normalize_file_url("  file:/tmp/a.odt  ") == "file:///tmp/a.odt"
    assert normalize_file_url("private:factory/swriter") == "private:factory/swriter"
    assert normalize_file_url("") == ""


def test_get_document_path_repairs_legacy_file_url_and_rejects_non_file():
    def to_system_path(url):
        assert url.startswith("file://")
        return url[len("file://") :]

    with patch("plugin.doc.text_helpers.uno.fileUrlToSystemPath", side_effect=to_system_path):
        model = MagicMock()
        model.getURL.return_value = "file:/tmp/legacy.odt"
        assert get_document_path(model) == "/tmp/legacy.odt"

        model.getURL.return_value = "file:///tmp/ok.odt"
        assert get_document_path(model) == "/tmp/ok.odt"

        model.getURL.return_value = ""
        assert get_document_path(model) is None

        model.getURL.return_value = "private:factory/swriter"
        assert get_document_path(model) is None


class _Enum:
    def __init__(self, items):
        self._items = list(items)
        self._idx = 0

    def hasMoreElements(self):
        return self._idx < len(self._items)

    def nextElement(self):
        item = self._items[self._idx]
        self._idx += 1
        return item


class _Portion:
    def __init__(self, text="", portion_type="Text", redline_type=None):
        self._text = text
        self._portion_type = portion_type
        self._redline_type = redline_type

    def getPropertyValue(self, name):
        if name == "TextPortionType":
            return self._portion_type
        if name == "RedlineType":
            return self._redline_type
        raise Exception(name)

    def getString(self):
        return self._text


class _Paragraph:
    def __init__(self, portions, fallback_text=""):
        self._portions = portions
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._portions)

    def getString(self):
        return self._fallback_text


class _TextRange:
    def __init__(self, paragraphs, fallback_text=""):
        self._paragraphs = paragraphs
        self._fallback_text = fallback_text

    def createEnumeration(self):
        return _Enum(self._paragraphs)

    def getString(self):
        return self._fallback_text


def test_get_string_without_tracked_deletions_skips_deleted_portions():
    text_range = _TextRange(
        [
            _Paragraph(
                [
                    _Portion("Keep "),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("remove me"),
                    _Portion(portion_type="Redline", redline_type="Delete"),
                    _Portion("text"),
                ],
                fallback_text="Keep remove metext",
            ),
            _Paragraph([_Portion("Next line")], fallback_text="Next line"),
        ],
        fallback_text="Keep remove metext\nNext line",
    )

    assert get_string_without_tracked_deletions(text_range) == "Keep text\nNext line"


def test_get_full_writer_text_truncates_and_reads_prefix():
    with (
        patch("plugin.doc.text_helpers._writer_char_count", return_value=20),
        patch("plugin.doc.text_helpers._read_writer_text_slice", return_value="abcdefghij") as mock_read,
    ):
        out = get_full_writer_text(object(), max_chars=10)
    mock_read.assert_called_once()
    assert mock_read.call_args.args[1:] == (0, 10)
    assert out.endswith("[... document truncated ...]")
    assert out.startswith("abcdefghij")


def test_get_full_writer_text_short_doc_has_no_truncation_note():
    with (
        patch("plugin.doc.text_helpers._writer_char_count", return_value=5),
        patch("plugin.doc.text_helpers._read_writer_text_slice", return_value="hello"),
    ):
        assert get_full_writer_text(object(), max_chars=10) == "hello"


def test_text_helpers_import_does_not_load_calc_analyzer():
    """LibrePy-style import: text_helpers must not pull SheetAnalyzer / CalcBridge."""
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    code = (
        "import sys, types\n"
        "if 'pyuno' not in sys.modules:\n"
        "    _mod = types.ModuleType('pyuno')\n"
        "    _mod.getComponentContext = lambda: None\n"
        "    sys.modules['pyuno'] = _mod\n"
        "import plugin.doc.text_helpers\n"
        "assert 'plugin.calc.analyzer' not in sys.modules\n"
        "assert 'plugin.calc.bridge' not in sys.modules\n"
        "assert 'plugin.doc.document_helpers' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, result.stdout + result.stderr
