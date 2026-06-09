# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for built-in vision script templates."""

from __future__ import annotations

from plugin.scripting.vision import HELPER_NAMES
from plugin.scripting.vision_templates import (
    VISION_HEADER_PREFIX,
    get_vision_script_templates,
    parse_vision_script_header,
)


def test_templates_cover_shipped_helpers():
    templates = get_vision_script_templates()
    assert set(templates.keys()) == {"extract_text", "extract_structure"}
    assert "extract_text" in HELPER_NAMES
    assert "extract_structure" in HELPER_NAMES


def test_parse_header_round_trip():
    templates = get_vision_script_templates()
    code = templates["extract_text"]
    assert VISION_HEADER_PREFIX in code
    meta = parse_vision_script_header(code)
    assert meta is not None
    assert meta.helper == "extract_text"
    assert meta.params == {
        "engine": "docling",
        "ocr_backend": "rapidocr",
        "image_name": "",
    }


def test_parse_header_with_params():
    code = '# writeragent:vision helper=extract_text params={"lang":"fr"}\nresult = 1\n'
    meta = parse_vision_script_header(code)
    assert meta is not None
    assert meta.helper == "extract_text"
    assert meta.params == {"lang": "fr"}


def test_parse_header_rejects_unknown_helper():
    code = "# writeragent:vision helper=not_real params={}\n"
    assert parse_vision_script_header(code) is None


def test_parse_header_accepts_future_helper_name():
    code = "# writeragent:vision helper=detect_objects params={}\n"
    meta = parse_vision_script_header(code)
    assert meta is not None
    assert meta.helper == "detect_objects"


def test_parse_header_with_image_name():
    code = '# writeragent:vision helper=extract_text params={"lang":"en","image_name":"Photo1"}\n'
    meta = parse_vision_script_header(code)
    assert meta is not None
    assert meta.params == {"lang": "en", "image_name": "Photo1"}


def test_extract_structure_template_round_trip():
    templates = get_vision_script_templates()
    code = templates["extract_structure"]
    meta = parse_vision_script_header(code)
    assert meta is not None
    assert meta.helper == "extract_structure"
    assert meta.params.get("image_name") == ""
