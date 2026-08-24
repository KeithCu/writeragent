import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(PROJECT_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from generate_config_schema_docs import (  # noqa: E402
    SCHEMA_DOC_URL,
    load_core_config_fields,
    render_config_schema_markdown,
)


class TestGenerateConfigSchemaDocs(unittest.TestCase):
    def test_render_includes_keys_and_defaults(self):
        modules = [
            {
                "name": "chatbot",
                "title": "Sidebar",
                "config": {
                    "max_tool_rounds": {
                        "type": "int",
                        "default": 15,
                        "min": 1,
                        "max": 50,
                        "label": "Max Tool Rounds",
                    },
                    "log_level": {
                        "type": "string",
                        "default": "DEBUG",
                        "internal": True,
                        "options": [{"value": "DEBUG", "label": "Debug"}],
                    },
                },
            }
        ]
        md = render_config_schema_markdown(modules, core_fields=[])
        self.assertIn("Auto-generated", md)
        self.assertIn(SCHEMA_DOC_URL, md)
        self.assertIn("/blob/master/", SCHEMA_DOC_URL)
        self.assertNotIn("/blob/main/", SCHEMA_DOC_URL)
        self.assertIn("| Key | Type | Default | Range | Description |", md)
        self.assertIn("| `max_tool_rounds` | `int` | `15` | `1`–`50` | Max Tool Rounds |", md)
        self.assertIn("| `log_level` |", md)
        self.assertIn("Internal", md)
        self.assertIn("DEBUG (Debug)", md)
        self.assertNotIn("widget", md.lower())
        self.assertNotIn("public", md.lower())

    def test_skips_modules_without_config(self):
        md = render_config_schema_markdown(
            [{"name": "empty", "title": "Empty"}], core_fields=[]
        )
        self.assertNotIn("## Empty", md)
        self.assertIn("writeragent.json settings", md)

    def test_core_fields_include_tunables_omit_secrets(self):
        fields = load_core_config_fields()
        names = {f["name"] for f in fields}
        self.assertIn("endpoint", names)
        self.assertIn("chat_max_tokens", names)
        self.assertIn("parallel_tool_calls", names)
        self.assertNotIn("api_keys_by_endpoint", names)
        self.assertNotIn("saved_python_scripts", names)
        self.assertNotIn("model", names)
        self.assertNotIn("_extra_config", names)
        self.assertNotIn("last_latex_input", names)
        endpoint = next(f for f in fields if f["name"] == "endpoint")
        self.assertEqual(endpoint["default"], "http://localhost:11434")

    def test_skips_dialog_chrome_and_public_flag(self):
        md = render_config_schema_markdown(
            [
                {
                    "name": "mcp",
                    "title": "MCP",
                    "config": {
                        "mcp_port": {
                            "type": "int",
                            "default": 8765,
                            "public": True,
                            "helper": "Listen port",
                            "widget": "number",
                        },
                        "_sep_tunnel": {"widget": "separator"},
                        "copy_config": {"widget": "button", "label": "Copy"},
                    },
                }
            ],
            core_fields=[],
        )
        self.assertIn("| `mcp_port` |", md)
        self.assertIn("Listen port", md)
        self.assertNotIn("_sep_tunnel", md)
        self.assertNotIn("copy_config", md)
        self.assertNotIn("true", md)
        self.assertNotIn("number", md)

    def test_render_merges_core_before_yaml(self):
        md = render_config_schema_markdown(
            [
                {
                    "name": "chatbot",
                    "title": "Sidebar",
                    "config": {"max_tool_rounds": {"type": "int", "default": 15}},
                }
            ],
            core_fields=[
                {"name": "endpoint", "type": "string", "default": "http://localhost:11434"}
            ],
        )
        self.assertIn("## Core (`WriterAgentConfig`)", md)
        self.assertLess(md.find("| `endpoint` |"), md.find("| `max_tool_rounds` |"))
        self.assertNotIn("api_keys_by_endpoint", md)
        self.assertNotIn("Widget", md)
