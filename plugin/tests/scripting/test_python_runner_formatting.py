# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import unittest
from plugin.scripting.python_runner import format_result_for_writer

class TestPythonRunnerFormatting(unittest.TestCase):
    def test_format_string(self):
        self.assertEqual(format_result_for_writer("hello"), "hello")
        self.assertEqual(format_result_for_writer(123), "123")

    def test_format_zero(self):
        self.assertEqual(format_result_for_writer(0), "0")
        self.assertEqual(format_result_for_writer(0.0), "0.0")

    def test_format_list_of_lists(self):
        data = [["A", "B"], [1, 2]]
        expected = '<table border="1"><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>'
        self.assertEqual(format_result_for_writer(data), expected)

    def test_format_list_of_dicts(self):
        data = [{"Name": "Alice", "Age": 30}, {"Name": "Bob", "Age": 25}]
        expected = '<table border="1"><thead><tr><th>Name</th><th>Age</th></tr></thead><tbody><tr><td>Alice</td><td>30</td></tr><tr><td>Bob</td><td>25</td></tr></tbody></table>'
        self.assertEqual(format_result_for_writer(data), expected)

    def test_format_complex_dict(self):
        data = {
            "summary_text": "Hello\nWorld",
            "data": [{"A": 1}],
            "total": 100
        }
        # summary_text should be first, no bold label.
        # data should be table.
        # total should be bold label.
        res = format_result_for_writer(data)
        self.assertIn("<p>Hello<br>World</p>", res)
        self.assertIn("<h3>data</h3>", res)
        self.assertIn('<table border="1">', res)
        self.assertIn("<p><b>total:</b> 100</p>", res)

    def test_empty_or_none(self):
        self.assertEqual(format_result_for_writer(None), "")
        self.assertEqual(format_result_for_writer([]), "")
        self.assertEqual(format_result_for_writer(""), "")

if __name__ == "__main__":
    unittest.main()
