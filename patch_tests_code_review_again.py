import re

with open('tests/writer/locale/test_harper.py', 'r') as f:
    content = f.read()

# Add test for "a\u2028b teh" to test_harper.py
test_case = """
def test_lsp_range_to_offset_unicode_line_separator() -> None:
    text = "a\\u2028b teh"
    assert lsp_range_to_offset(text, 0, 0) == 0
    assert lsp_range_to_offset(text, 0, 2) == 2
"""
if "test_lsp_range_to_offset_unicode_line_separator" not in content:
    content += test_case

content = re.sub(
    r'(def test_harper_try_lint_eof_returns_none.*?assert harper_try_lint.*?is None\s*\n\s*)assert mock_bg.call_count == 1\s*\n\s*assert harper_module\._HARPER_STATE is HarperRuntimeState\.RESOLVING',
    r'\1assert mock_bg.call_count == 0\n    assert harper_module._HARPER_STATE is HarperRuntimeState.FAILED',
    content,
    flags=re.DOTALL
)

content = re.sub(
    r'(def test_harper_try_lint_logs_error_on_lint_exception.*?assert any.*?in caplog\.records\)\s*\n\s*)assert mock_bg\.call_count == 1',
    r'\1assert mock_bg.call_count == 0\n    assert harper_module._HARPER_STATE is HarperRuntimeState.FAILED',
    content,
    flags=re.DOTALL
)

with open('tests/writer/locale/test_harper.py', 'w') as f:
    f.write(content)
