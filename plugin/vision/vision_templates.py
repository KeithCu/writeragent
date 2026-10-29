# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted vision helpers."""

from __future__ import annotations

from plugin.scripting.helper_domain import HelperScriptMeta, parse_helper_script_header
from plugin.vision.vision_common import HELPER_NAMES, IMPLEMENTED_HELPERS

_HELPER_DESCRIPTIONS: dict[str, str] = {"extract_text": "OCR selected image(s) to formatted HTML (Docling default, Paddle fallback).", "extract_structure": "Layout and tables as formatted HTML with cell spans — Docling default, Paddle fallback."}


def _template_body(helper: str) -> str:
    desc = _HELPER_DESCRIPTIONS[helper]
    return "\n".join([f"# {desc}", "# Select an image (or a Writer range with multiple images), then Run.", "from writeragent.vision import run_vision", f"result = run_vision({helper!r}, image)", ""])


def get_vision_script_templates() -> dict[str, str]:
    """Return built-in vision helper scripts keyed by helper name."""
    return {helper: _template_body(helper) for helper in sorted(IMPLEMENTED_HELPERS)}


def parse_vision_script_header(code: str) -> HelperScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied vision script."""
    return parse_helper_script_header(code, tag="vision", helper_names=HELPER_NAMES)
