import unittest
from unittest.mock import MagicMock, patch
import io
import csv

# Mock the parts of core that we don't want to load or that depend on UNO
import sys
from types import ModuleType

# Mock core.calc_address_utils
m = ModuleType("core.calc_address_utils")
m.parse_address = lambda x: (0, 0)
sys.modules["core.calc_address_utils"] = m

# Mock core.logging
m = ModuleType("core.logging")
m.debug_log = MagicMock()
sys.modules["core.logging"] = m

from plugin.modules.calc.manipulator import CellManipulator

class TestCSVImportLogic(unittest.TestCase):
    def setUp(self):
        self.bridge = MagicMock()
        self.manipulator = CellManipulator(self.bridge)
        self.sheet = MagicMock()
        self.bridge.get_active_sheet.return_value = self.sheet
        self.bridge._index_to_column.return_value = "A"

    def test_detect_comma(self):
        csv_data = "Name,Age,Country\nJohn,28,USA"
        with patch('csv.reader', side_effect=csv.reader) as mock_reader:
            self.manipulator.import_csv_from_string(csv_data)
            # Check if csv.reader was called with delimiter=','
            args, kwargs = mock_reader.call_args
            self.assertEqual(kwargs['delimiter'], ',')

    def test_detect_semicolon(self):
        csv_data = "Name;Age;Country\nJohn;28;USA"
        with patch('csv.reader', side_effect=csv.reader) as mock_reader:
            self.manipulator.import_csv_from_string(csv_data)
            # Check if csv.reader was called with delimiter=';'
            args, kwargs = mock_reader.call_args
            self.assertEqual(kwargs['delimiter'], ';')

    def test_mixed_prefers_comma(self):
        # If both are present, we currently default to comma or whatever the logic does.
        # My logic says: if ";" in first_line and "," not in first_line -> ";"
        # So mixed should be ","
        csv_data = "Name,Age;Country\nJohn,28;USA"
        with patch('csv.reader', side_effect=csv.reader) as mock_reader:
            self.manipulator.import_csv_from_string(csv_data)
            args, kwargs = mock_reader.call_args
            self.assertEqual(kwargs['delimiter'], ',')

    def test_no_delimiter_defaults_to_comma(self):
        csv_data = "NameAgeCountry\nJohn28USA"
        with patch('csv.reader', side_effect=csv.reader) as mock_reader:
            self.manipulator.import_csv_from_string(csv_data)
            args, kwargs = mock_reader.call_args
            self.assertEqual(kwargs['delimiter'], ',')

class TestFormulaParsingLogic(unittest.TestCase):
    def test_parse_json_array(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = '["A"; "B"; "C"]'
        self.assertEqual(_parse_formula_or_values_string(s), ["A", "B", "C"])

    def test_parse_raw_semicolon(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = "Name;Age;Country;Salary"
        self.assertEqual(_parse_formula_or_values_string(s), ["Name", "Age", "Country", "Salary"])

    def test_complex_formula_in_json_array(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = '["Highest Consumer"; "=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))"]'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "Highest Consumer")
        # Ensure the semicolon inside the formula is NOT replaced by a comma
        self.assertEqual(result[1], "=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))")

    def test_complex_formula_in_raw_string(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        # If the AI sends a raw string containing a semicolon-delimited list of values
        # where one value is a formula. NOTE: This is less common but we handle it via csv.reader.
        s = 'ID; "=INDEX(A2:A11;MATCH(1;B2:B11;0))"'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "ID")
        self.assertEqual(result[1], "=INDEX(A2:A11;MATCH(1;B2:B11;0))")

    def test_formula_not_split(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = "=SUM(A1;A2)"
        # Should return None so it's treated as a single formula string
        self.assertIsNone(_parse_formula_or_values_string(s))

    def test_single_value_not_split(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = "Plain text"
        self.assertIsNone(_parse_formula_or_values_string(s))

    def test_nested_json_arrays(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = '[["r1c1"; "r1c2"]; ["r2c1"; "r2c2"]]'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["r1c1", "r1c2", "r2c1", "r2c2"])

    def test_unicode_and_emoji(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = '["Česká"; "Republika"; "📈"]'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["Česká", "Republika", "📈"])

    def test_quoted_semicolon_in_formula_json(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        # Escaped quotes inside JSON string
        s = '["=IF(A1=\\";\\"; 1; 0)"]'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["=IF(A1=\";\"; 1; 0)"])

    def test_trailing_spaces_in_json(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = '  [  "A"  ;  "B"  ]  '
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["A", "B"])

    def test_raw_string_with_quotes_and_semis(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = 'Normal; "Quoted;Semi"; "Formula;=A1"'
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["Normal", "Quoted;Semi", "Formula;=A1"])

    def test_empty_fields_detection(self):
        from plugin.modules.calc.manipulator import _parse_formula_or_values_string
        s = "a;;b"
        result = _parse_formula_or_values_string(s)
        self.assertEqual(result, ["a", "", "b"])

if __name__ == "__main__":
    unittest.main()
