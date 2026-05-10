# WriterAgent — unit tests for Impress header/footer tool arg handling

import unittest

from plugin.modules.draw.headers_footers import _coerce_bool_arg


class TestCoerceBoolArg(unittest.TestCase):
    def test_bool_passthrough(self):
        self.assertIs(_coerce_bool_arg({"is_master_page": True}, "is_master_page"), True)
        self.assertIs(_coerce_bool_arg({"is_master_page": False}, "is_master_page"), False)

    def test_string_json(self):
        self.assertTrue(_coerce_bool_arg({"is_master_page": "true"}, "is_master_page"))
        self.assertTrue(_coerce_bool_arg({"is_master_page": "1"}, "is_master_page"))
        self.assertFalse(_coerce_bool_arg({"is_master_page": "false"}, "is_master_page"))
        self.assertFalse(_coerce_bool_arg({"is_master_page": ""}, "is_master_page"))

    def test_missing_defaults_false(self):
        self.assertFalse(_coerce_bool_arg({}, "is_master_page"))


if __name__ == "__main__":
    unittest.main()
