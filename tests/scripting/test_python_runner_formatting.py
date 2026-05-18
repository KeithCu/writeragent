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

    def test_format_complex_dict_order(self):
        # We now respect insertion order strictly.
        data = {
            "title": "My Title",
            "data": [{"A": 1}],
            "total": 100,
            "summary_text": "Finish"
        }
        res = format_result_for_writer(data)
        
        # Order should be: title, data, total, summary_text
        title_idx = res.find("My Title")
        data_idx = res.find("data")
        total_idx = res.find("total")
        summary_idx = res.find("Finish")
        
        self.assertLess(title_idx, data_idx)
        self.assertLess(data_idx, total_idx)
        self.assertLess(total_idx, summary_idx)
        
        # Priority keys (title, summary_text) should NOT have labels
        # but SHOULD be bold
        self.assertIn("<p><b>My Title</b></p>", res)
        self.assertIn("<p><b>Finish</b></p>", res)
        self.assertNotIn("<b>title:</b>", res)
        self.assertNotIn("<b>summary_text:</b>", res)
        # Non-priority keys SHOULD have labels
        self.assertIn("<b>total:</b>", res)

    def test_format_priority_keys_non_string(self):
        data = {
            "title": 12345,
            "summary": 99.9,
            "message": True,
            "result": {"nested": "value"}
        }
        res = format_result_for_writer(data)
        self.assertIn("<p><b>12345</b></p>", res)
        self.assertIn("<p><b>99.9</b></p>", res)
        self.assertIn("<p><b>True</b></p>", res)
        self.assertIn("<p><b>{'nested': 'value'}</b></p>", res)
        self.assertNotIn("<b>title:</b>", res)
        self.assertNotIn("<b>summary:</b>", res)
        self.assertNotIn("<b>message:</b>", res)
        self.assertNotIn("<b>result:</b>", res)

    def test_empty_or_none(self):
        self.assertEqual(format_result_for_writer(None), "")
        self.assertEqual(format_result_for_writer([]), "")
        self.assertEqual(format_result_for_writer(""), "")

if __name__ == "__main__":
    unittest.main()
