# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted vision helpers."""

from __future__ import annotations

import json
from typing import Any

from plugin.scripting.helper_domain import HelperScriptMeta, header_prefix, parse_helper_script_header
from plugin.vision.venv.vision import HELPER_NAMES

VISION_HEADER_PREFIX = header_prefix("vision")

_SHIPPED_TEMPLATES = frozenset({"extract_text", "extract_structure"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "extract_text": {
        "engine": "docling",
        "ocr_backend": "rapidocr",
        "image_name": "",
    },
    "extract_structure": {
        "engine": "docling",
        "ocr_backend": "rapidocr",
        "image_name": "",
    },
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "extract_text": "OCR selected image to formatted HTML (Docling default, Paddle fallback).",
    "extract_structure": "Layout and tables as formatted HTML — Docling default, Paddle fallback.",
}


VisionScriptMeta = HelperScriptMeta


def _template_body(helper: str, params: dict[str, Any]) -> str:
    # Vision body is custom (image arg + multi-line comments); keep explicit template.
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{VISION_HEADER_PREFIX} helper={helper} params={params_json}\n"  # nosec
        f"# {desc}\n"
        f"# Select an embedded graphic OR set image_name in params (from list_images).\n"
        f"# Writer: select the embedded graphic (OCR inserts after it). Calc: select cell-anchored graphic.\n"
        f"from writeragent.vision.venv.vision import run_vision\n\n"
        f"result = run_vision(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    image,\n"
        f"    {{}},\n"
        f")\n"
    )


def get_vision_script_templates() -> dict[str, str]:
    """Return built-in vision helper scripts keyed by helper name."""
    return {
        helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
        for helper in sorted(_SHIPPED_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_vision_script_header(code: str) -> VisionScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied vision script."""
    return parse_helper_script_header(code, tag="vision", helper_names=HELPER_NAMES)
