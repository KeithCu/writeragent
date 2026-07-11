# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted vision helpers."""

from __future__ import annotations

from typing import Any

from plugin.scripting.helper_domain import HelperScriptMeta, build_helper_script_template, header_prefix, parse_helper_script_header
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
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return build_helper_script_template(
        tag="vision",
        helper=helper,
        params=params,
        description=desc,
        style="run_import",
        import_module="writeragent.vision.venv.vision",
        run_name="run_vision",
        data_expr="image",
        context_expr="{}",
        extra_comment_lines=(
            "# Select an embedded graphic OR set image_name in params (from list_images).",
            "# Writer: select the embedded graphic (OCR inserts after it). Calc: select cell-anchored graphic.",
        ),
        compact_json=True,
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
