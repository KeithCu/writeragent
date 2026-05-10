import unittest
import json
from plugin.framework.errors import safe_python_literal_eval

class TestSecurityFix(unittest.TestCase):
    def test_nested_structures_no_crash(self):
        # Deeply nested structure that would cause SyntaxError or crash with ast.literal_eval
        # Python 3.12 json.loads can handle depth 1000, but let's go deeper or mock it
        # to ensure RecursionError is caught.
        depth = 5000
        nested_list_str = "[" * depth + "]" * depth

        # safe_python_literal_eval should NOT crash.
        try:
            result = safe_python_literal_eval(nested_list_str, default="fallback")
            # In some environments, json.loads might throw RecursionError
            # Our code should catch it and return "fallback"
            self.assertTrue(isinstance(result, list) or result == "fallback")
        except Exception as e:
            self.fail(f"safe_python_literal_eval crashed with {type(e).__name__}: {e}")

    def test_large_input_no_crash(self):
        # Very large input (2MB)
        large_input = "[" + "1," * 1000000 + "1]"
        try:
            result = safe_python_literal_eval(large_input, default="fallback")
            self.assertTrue(isinstance(result, list) or result == "fallback")
        except Exception as e:
            self.fail(f"safe_python_literal_eval crashed with {type(e).__name__}: {e}")

    def test_common_literals(self):
        self.assertEqual(safe_python_literal_eval("True"), True)
        self.assertEqual(safe_python_literal_eval("true"), True)
        self.assertEqual(safe_python_literal_eval("False"), False)
        self.assertEqual(safe_python_literal_eval("false"), False)
        self.assertEqual(safe_python_literal_eval("None"), None)
        self.assertEqual(safe_python_literal_eval("none"), None)
        self.assertEqual(safe_python_literal_eval("null"), None)
        self.assertEqual(safe_python_literal_eval("NULL"), None)
        self.assertEqual(safe_python_literal_eval("123"), 123)
        self.assertEqual(safe_python_literal_eval('"hello"'), "hello")
        self.assertEqual(safe_python_literal_eval("'hello'"), "hello")

    def test_json_structures(self):
        self.assertEqual(safe_python_literal_eval('[1, 2, 3]'), [1, 2, 3])
        self.assertEqual(safe_python_literal_eval('{"a": 1}'), {"a": 1})

    def test_single_quoted_strings_restricted(self):
        self.assertEqual(safe_python_literal_eval("'safe'"), "safe")
        self.assertEqual(safe_python_literal_eval("'it\\'s unsafe'", default="fallback"), "fallback")

    def test_non_json_python_literals_fallback(self):
        self.assertEqual(safe_python_literal_eval("(1, 2)", default="(1, 2)"), "(1, 2)")
        self.assertEqual(safe_python_literal_eval("{'a': 1}", default="fallback"), "fallback")

    def test_glm45_deserializer(self):
        from plugin.contrib.tool_call_parsers.glm45_parser import _deserialize_value
        self.assertEqual(_deserialize_value("True"), True)
        self.assertEqual(_deserialize_value("true"), True)
        self.assertEqual(_deserialize_value("123"), 123)
        self.assertEqual(_deserialize_value("'abc'"), "abc")

    def test_qwen3_coder_deserializer(self):
        from plugin.contrib.tool_call_parsers.qwen3_coder_parser import _try_convert_value
        self.assertEqual(_try_convert_value("True"), True)
        self.assertEqual(_try_convert_value("null"), None)
        self.assertEqual(_try_convert_value("123"), 123)

    def test_smolagents_deserializer(self):
        self.assertEqual(safe_python_literal_eval('{"type": "string"}'), {"type": "string"})

if __name__ == "__main__":
    unittest.main()
