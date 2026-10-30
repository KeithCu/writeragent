# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for built-in vision script templates."""

from __future__ import annotations

from plugin.scripting.helper_domain import parse_run_import_call_spec
from plugin.vision.vision_common import HELPER_NAMES
from plugin.vision.vision_templates import get_vision_script_templates


def test_templates_cover_shipped_helpers():
    templates = get_vision_script_templates()
    assert set(templates.keys()) == {"extract_text", "extract_structure"}
    assert "extract_text" in HELPER_NAMES
    assert "extract_structure" in HELPER_NAMES


def test_templates_are_executable_run_import():
    templates = get_vision_script_templates()
    code = templates["extract_text"]
    assert "run_vision" in code
    assert "image" in code
    assert "# writeragent:vision" not in code
    call_spec = parse_run_import_call_spec(code, run_name="run_vision")
    assert call_spec is not None
    assert call_spec.get("helper") == "extract_text"
    params = call_spec.get("params")
    assert isinstance(params, dict)
    assert params == {}


def test_extract_structure_template_is_executable():
    templates = get_vision_script_templates()
    code = templates["extract_structure"]
    call_spec = parse_run_import_call_spec(code, run_name="run_vision")
    assert call_spec is not None
    assert call_spec.get("helper") == "extract_structure"
    params = call_spec.get("params")
    assert isinstance(params, dict)
    assert params == {}

