# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for built-in vision script templates."""

from __future__ import annotations

from plugin.scripting.helper_domain import ScriptFacts
from plugin.vision.vision_templates import get_vision_script_templates


def test_templates_are_executable_run_import():
    templates = get_vision_script_templates()
    code = templates["extract_text"]
    assert "run_vision" in code
    assert "image" in code
    assert "# writeragent:vision" not in code
    call_spec = ScriptFacts.from_source(code).first_run_spec("run_vision")
    assert call_spec is not None
    assert call_spec.get("helper") == "extract_text"
    params = call_spec.get("params")
    assert params == {}


def test_extract_structure_template_is_executable():
    templates = get_vision_script_templates()
    code = templates["extract_structure"]
    call_spec = ScriptFacts.from_source(code).first_run_spec("run_vision")
    assert call_spec is not None
    assert call_spec.get("helper") == "extract_structure"
    params = call_spec.get("params")
    assert params == {}

