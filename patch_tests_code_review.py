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

with open('tests/writer/locale/test_harper.py', 'w') as f:
    f.write(content)
