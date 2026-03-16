import sys
import os
import re

# Add core to path
sys.path.insert(0, os.getcwd())

from plugin.modules.writer.format_support import _ensure_html_linebreaks, _strip_html_boilerplate

def test_conversion():
    # 1. Plain text with single newlines
    inp = "Line 1\nLine 2"
    expected_body = "<p>Line 1<br>\nLine 2</p>"
    result = _ensure_html_linebreaks(inp)
    assert expected_body in result, f"Test 1 failed: expected body {repr(expected_body)} to be in result {repr(result)}"
    assert "<!DOCTYPE html>" in result
    
    # 2. Plain text with double newlines
    inp = "Para 1\n\nPara 2"
    expected_body = "<p>Para 1</p>\n<p>Para 2</p>"
    result = _ensure_html_linebreaks(inp)
    assert expected_body in result, f"Test 2 failed: expected body {repr(expected_body)} to be in result {repr(result)}"
    
    # 3. Text with basic HTML tags (should be untouched, but wrapped)
    inp = "<h1>Title</h1>\n<p>Body</p>"
    result = _ensure_html_linebreaks(inp)
    assert inp in result, f"Test 3 failed: expected {repr(inp)} to be in result {repr(result)}"
    
    # 4. Mix of single and double newlines
    inp = "Title\nSubtitle\n\nMain content\nMore content"
    expected_body = "<p>Title<br>\nSubtitle</p>\n<p>Main content<br>\nMore content</p>"
    result = _ensure_html_linebreaks(inp)
    assert expected_body in result, f"Test 4 failed: expected body {repr(expected_body)} to be in result {repr(result)}"

def test_stripping():
    # 5. Full HTML document stripping
    inp = '<html><head><style>body { color: red; }</style></head><body lang="en-US"><h1>Hello</h1><p>World</p></body></html>'
    expected = '<h1>Hello</h1><p>World</p>'
    result = _strip_html_boilerplate(inp)
    assert result == expected, f"Test 5 failed: expected {repr(expected)}, got {repr(result)}"
    
    # 6. Upper case tags
    inp = '<BODY>Upper Case</BODY>'
    expected = 'Upper Case'
    result = _strip_html_boilerplate(inp)
    assert result == expected, f"Test 6 failed: expected {repr(expected)}, got {repr(result)}"

if __name__ == "__main__":
    try:
        test_conversion()
        print("Linebreak conversion tests passed!")
        test_stripping()
        print("Boilerplate stripping tests passed!")
        print("\nAll HTML formatting unit tests passed!")
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
