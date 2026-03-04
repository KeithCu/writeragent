"""Tests for plugin.framework.config_schema (build-time XCS/XCU generation)."""

import pytest

from plugin.framework.config_schema import generate_xcs, generate_xcu


SAMPLE_CONFIG = {
    "enabled": {
        "type": "boolean",
        "default": False,
        "widget": "checkbox",
        "label": "Enable Feature",
        "public": True,
    },
    "port": {
        "type": "int",
        "default": 8766,
        "min": 1024,
        "max": 65535,
        "widget": "number",
        "label": "Server Port",
        "description": "TCP port for the server",
    },
    "name": {
        "type": "string",
        "default": "hello & <world>",
        "widget": "text",
        "label": "Name",
    },
}


class TestGenerateXcs:
    def test_basic_structure(self):
        xcs = generate_xcs("mcp", SAMPLE_CONFIG)
        assert "<?xml version=" in xcs
        assert 'oor:name="mcp"' in xcs
        assert 'oor:package="org.localwriter.mcp"' in xcs
        assert "<oor:component-schema" in xcs
        assert "<component>" in xcs

    def test_boolean_type(self):
        xcs = generate_xcs("mcp", SAMPLE_CONFIG)
        assert 'oor:name="enabled"' in xcs
        assert 'oor:type="xs:boolean"' in xcs

    def test_int_type(self):
        xcs = generate_xcs("mcp", SAMPLE_CONFIG)
        assert 'oor:name="port"' in xcs
        assert 'oor:type="xs:int"' in xcs

    def test_description_used_when_present(self):
        xcs = generate_xcs("mcp", SAMPLE_CONFIG)
        assert "TCP port for the server" in xcs

    def test_label_fallback_when_no_description(self):
        xcs = generate_xcs("mcp", SAMPLE_CONFIG)
        assert "Enable Feature" in xcs  # label used as desc fallback

    def test_xml_escaping_in_field_names(self):
        config = {"a<b": {"type": "string", "default": "", "label": "Test"}}
        xcs = generate_xcs("test", config)
        assert "a&lt;b" in xcs


class TestGenerateXcu:
    def test_basic_structure(self):
        xcu = generate_xcu("mcp", SAMPLE_CONFIG)
        assert "<?xml version=" in xcu
        assert 'oor:name="mcp"' in xcu
        assert 'oor:package="org.localwriter.mcp"' in xcu
        assert "<oor:component-data" in xcu

    def test_boolean_default(self):
        xcu = generate_xcu("mcp", SAMPLE_CONFIG)
        assert 'xsi:type="xs:boolean">false</value>' in xcu

    def test_int_default(self):
        xcu = generate_xcu("mcp", SAMPLE_CONFIG)
        assert 'xsi:type="xs:int">8766</value>' in xcu

    def test_string_default_xml_escaped(self):
        xcu = generate_xcu("mcp", SAMPLE_CONFIG)
        assert "hello &amp; &lt;world&gt;" in xcu

    def test_empty_config_produces_valid_xml(self):
        xcu = generate_xcu("empty", {})
        assert "<oor:component-data" in xcu
        assert "</oor:component-data>" in xcu
