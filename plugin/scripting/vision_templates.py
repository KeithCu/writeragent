# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted vision helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting.vision import HELPER_NAMES

VISION_HEADER_PREFIX = "# writeragent:vision"
_VISION_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:vision\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

# Phase 1 ships extract_text only; other helpers appear in HELPER_NAMES for future templates.
_PHASE1_TEMPLATES = frozenset({"extract_text"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "extract_text": {},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "extract_text": "OCR selected image — click the graphic, place cursor for insert, then Run.",
}


@dataclass(frozen=True)
class VisionScriptMeta:
    helper: str
    params: dict[str, Any]


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{VISION_HEADER_PREFIX} helper={helper} params={params_json}\n"
        f"# {desc}\n"
        f"# Select an embedded graphic, place the text cursor for insert, then Run.\n"
        f"from plugin.scripting.vision import run_vision\n\n"
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
        for helper in sorted(_PHASE1_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_vision_script_header(code: str) -> VisionScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied vision script."""
    if not code or VISION_HEADER_PREFIX not in code:
        return None
    match = _VISION_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return VisionScriptMeta(helper=helper, params=params)
