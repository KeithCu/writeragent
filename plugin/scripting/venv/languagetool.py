# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted LanguageTool grammar checker executing inside the user's virtual environment."""

import logging
from typing import Any, Dict

LANGUAGETOOL_RULE_PREFIX = "languagetool||"

# Global cache of initialized LanguageTool clients to avoid JVM startup/restart overhead
_LT_CACHE: Dict[str, Any] = {}
log = logging.getLogger("writeragent.grammar")


def run_languagetool_check(text: str, bcp47: str) -> dict:
    """Execute grammar check on text using language_tool_python in the venv."""
    try:
        import language_tool_python
    except ImportError:
        raise RuntimeError(
            "The 'language-tool-python' package is not installed in the venv. "
            "Please run 'uv pip install language-tool-python' or equivalent in your configured virtual environment."
        )

    # Normalize locale code
    bcp47_clean = bcp47.replace("_", "-")

    if bcp47_clean not in _LT_CACHE:
        try:
            # Starts local Java server (or queries existing one)
            _LT_CACHE[bcp47_clean] = language_tool_python.LanguageTool(bcp47_clean)
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialize LanguageTool server for locale {bcp47_clean}. "
                f"Ensure that Java (JRE) is installed and available in the system PATH. Error: {e}"
            )

    tool = _LT_CACHE[bcp47_clean]

    try:
        matches = tool.check(text)
        errors = []
        for m in matches:
            # Replicate standard schema expected by WriterAgent grammar underlines
            errors.append({
                "wrong": getattr(m, "matched_text", "") or (m.context[m.offset_in_context:m.offset_in_context + m.error_length] if m.context else ""),
                "correct": m.replacements[0] if m.replacements else "",
                "n_error_start": m.offset,
                "n_error_length": m.error_length,
                "short_comment": m.message,
                "full_comment": m.sentence,
                "rule_identifier": f"{LANGUAGETOOL_RULE_PREFIX}{m.rule_id}",
                "suggestions": m.replacements[:5],
                "reason": m.message,
                "type": "LanguageTool",
            })
        return {"errors": errors}
    except Exception as e:
        raise RuntimeError(f"LanguageTool check failed: {e}")
