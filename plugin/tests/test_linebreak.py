from plugin.framework.document import normalize_linebreaks

def test_normalize_linebreaks():
    print("Running linebreak normalization tests...")
    
    # Simple cases
    assert normalize_linebreaks("Line 1\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\r\nLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\rLine 2") == "Line 1\nLine 2"
    assert normalize_linebreaks("Line 1\n\rLine 2") == "Line 1\nLine 2"
    
    # Mixed and multiple
    assert normalize_linebreaks("A\r\nB\rC\n\rD\nE") == "A\nB\nC\nD\nE"
    assert normalize_linebreaks("\r\n\r\n") == "\n\n"
    assert normalize_linebreaks("\n\r\n\r") == "\n\n"
    assert normalize_linebreaks("\r\r") == "\n\n"
    
    # Edge cases
    assert normalize_linebreaks("") == ""
    assert normalize_linebreaks(None) == ""
    
    print("All linebreak normalization tests PASSED!")

if __name__ == "__main__":
    test_normalize_linebreaks()
