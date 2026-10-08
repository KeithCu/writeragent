# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted vision helpers."""

from __future__ import annotations

from plugin.scripting.helper_domain import HelperScriptMeta, header_prefix, parse_helper_script_header
from plugin.vision.vision_common import HELPER_NAMES, IMPLEMENTED_HELPERS

VISION_HEADER_PREFIX = header_prefix("vision")

_HELPER_DESCRIPTIONS: dict[str, str] = {"extract_text": "OCR selected image(s) to formatted HTML (Docling default, Paddle fallback).", "extract_structure": "Layout and tables as formatted HTML with cell spans — Docling default, Paddle fallback."}


VisionScriptMeta = HelperScriptMeta


def _template_body(helper: str) -> str:
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    lines = [f"# {desc}", "# Select an image (or a Writer range with multiple images), then Run.", "from writeragent.vision import run_vision", f"result = run_vision({helper!r}, image)", ""]
    return "\n".join(lines)


def get_vision_script_templates() -> dict[str, str]:
    """Return built-in vision helper scripts keyed by helper name."""
    return {helper: _template_body(helper) for helper in sorted(IMPLEMENTED_HELPERS) if helper in HELPER_NAMES}


def parse_vision_script_header(code: str) -> VisionScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied vision script."""
    return parse_helper_script_header(code, tag="vision", helper_names=HELPER_NAMES)
