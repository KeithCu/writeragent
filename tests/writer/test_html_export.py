from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.writer.html_export import xtext_to_content


def test_xtext_to_content_none_is_empty():
    assert xtext_to_content(None, object(), object()) == ""


def test_ops_uno_skips_windows_leftover_text_offsets() -> None:
    """GHA 34689136372: leftover reuse offset mismatch, not a product range bug."""
    from pathlib import Path

    src = Path(__file__).with_name("test_ops_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "ops_uno leftover text offsets" in src
    assert "34689136372" in src


def test_html_export_uno_skips_windows_leftover_hidden_temp_doc() -> None:
    """GHA 34690797019: leftover Hidden _default hung on temp_doc.close."""
    from pathlib import Path

    src = Path(__file__).with_name("test_html_export_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "html_export Hidden _default temp_doc" in src
    assert "34690797019" in src
