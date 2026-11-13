import pytest

from plugin.calc.manipulator import _parse_formula_or_values_string


@pytest.mark.parametrize(
    ("text", "grid"),
    [
        ("Name,Age,Country\nJohn,28,USA", [["Name", "Age", "Country"], ["John", "28", "USA"]]),
        ("Name;Age;Country\nJohn;28;USA", [["Name", "Age", "Country"], ["John", "28", "USA"]]),
        ("Name,Age;Country\nJohn,28;USA", [["Name", "Age;Country"], ["John", "28;USA"]]),  # comma wins when both appear
        ("NameAgeCountry\nJohn28USA", [["NameAgeCountry"], ["John28USA"]]),  # no delimiter -> one column
    ],
)
def test_csv_text_delimiter_choice_gives_grid(text, grid):
    assert _parse_formula_or_values_string(text) == grid


class TestFormulaParsingLogic:
    def test_parse_json_array(self):
        s = '["A"; "B"; "C"]'
        assert (_parse_formula_or_values_string(s)) == (["A", "B", "C"])

    def test_parse_raw_semicolon(self):
        s = "Name;Age;Country;Salary"
        assert (_parse_formula_or_values_string(s)) == (["Name", "Age", "Country", "Salary"])

    def test_complex_formula_in_json_array(self):
        s = '["Highest Consumer"; "=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))"]'
        result = _parse_formula_or_values_string(s)
        assert (len(result)) == (2)
        assert (result[0]) == ("Highest Consumer")
        # Ensure the semicolon inside the formula is NOT replaced by a comma
        assert (result[1]) == ("=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))")

    def test_complex_formula_in_raw_string(self):
        # If the AI sends a raw string containing a semicolon-delimited list of values
        # where one value is a formula. NOTE: This is less common but we handle it via csv.reader.
        s = 'ID; "=INDEX(A2:A11;MATCH(1;B2:B11;0))"'
        result = _parse_formula_or_values_string(s)
        assert (len(result)) == (2)
        assert (result[0]) == ("ID")
        assert (result[1]) == ("=INDEX(A2:A11;MATCH(1;B2:B11;0))")

    def test_formula_not_split(self):
        s = "=SUM(A1;A2)"
        # Should return None so it's treated as a single formula string
        assert (_parse_formula_or_values_string(s)) is None

    def test_single_value_not_split(self):
        s = "Plain text"
        assert (_parse_formula_or_values_string(s)) is None

    def test_nested_json_arrays(self):
        s = '[["r1c1"; "r1c2"]; ["r2c1"; "r2c2"]]'
        result = _parse_formula_or_values_string(s)
        assert result == [["r1c1", "r1c2"], ["r2c1", "r2c2"]]

    def test_unicode_and_emoji(self):
        s = '["Česká"; "Republika"; "📈"]'
        result = _parse_formula_or_values_string(s)
        assert (result) == (["Česká", "Republika", "📈"])

    def test_quoted_semicolon_in_formula_json(self):
        # Escaped quotes inside JSON string
        s = '["=IF(A1=\\";\\"; 1; 0)"]'
        result = _parse_formula_or_values_string(s)
        assert (result) == (["=IF(A1=\";\"; 1; 0)"])

    def test_trailing_spaces_in_json(self):
        s = '  [  "A"  ;  "B"  ]  '
        result = _parse_formula_or_values_string(s)
        assert (result) == (["A", "B"])

    def test_raw_string_with_quotes_and_semis(self):
        s = 'Normal; "Quoted;Semi"; "Formula;=A1"'
        result = _parse_formula_or_values_string(s)
        assert (result) == (["Normal", "Quoted;Semi", "Formula;=A1"])

    def test_empty_fields_detection(self):
        s = "a;;b"
        result = _parse_formula_or_values_string(s)
        assert (result) == (["a", "", "b"])

    def test_single_cell_range_comma_prose_is_literal(self):
        s = "Hello ケイス, this is a test."
        assert (_parse_formula_or_values_string(s, single_cell_range=True)) is None
        assert (_parse_formula_or_values_string(s, single_cell_range=False)) == (["Hello ケイス", "this is a test."])

    def test_single_cell_range_comment_with_comma(self):
        s = "Note: see section 3, paragraph 2."
        assert (_parse_formula_or_values_string(s, single_cell_range=True)) is None

    def test_single_cell_range_semicolon_prose_is_literal(self):
        s = "Left; right"
        assert (_parse_formula_or_values_string(s, single_cell_range=True)) is None
        assert (_parse_formula_or_values_string(s, single_cell_range=False)) == (["Left", "right"])

