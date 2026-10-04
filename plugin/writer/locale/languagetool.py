# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted LanguageTool grammar checker executing inside the user's virtual environment."""

import logging
import threading
from typing import Any, Dict

from plugin.writer.locale.grammar_ignore_rules import LANGUAGETOOL_RULE_PREFIX, make_rule_identifier

# Global cache of initialized LanguageTool clients to avoid JVM startup/restart overhead.
# Check-then-set must be locked: two threads both missing the key used to each
# construct LanguageTool(), and the loser leaked an orphaned JVM.
_LT_CACHE: Dict[str, Any] = {}
_LT_CACHE_LOCK = threading.Lock()
log = logging.getLogger("writeragent.grammar")


def _match_wrong_text(m: Any) -> str:
    """Text the match covered.

    ``matched_text`` of ``""`` is falsy, so ``or context[start:end]`` used to
    take Python's lenient slice (negative start, end past the string) and
    store the wrong span. Any real string, including empty, wins. The context
    slice is only used when that attribute is missing and both offsets sit
    inside ``context``.
    """
    matched = getattr(m, "matched_text", None)
    # ``""`` is a real value. The old ``or slice`` treated it as missing and
    # underlined whatever a lenient context slice happened to return.
    if isinstance(matched, str):
        return matched
    context = getattr(m, "context", None)
    if not isinstance(context, str) or not context:
        return ""
    start = getattr(m, "offset_in_context", None)
    length = getattr(m, "error_length", None)
    if isinstance(start, bool) or isinstance(length, bool):
        return ""
    if not isinstance(start, int) or not isinstance(length, int):
        return ""
    if start < 0 or length < 0 or start + length > len(context):
        return ""
    return context[start : start + length]


def _cached_languagetool(bcp47_clean: str) -> Any:
    """Return the cached client, constructing at most one per locale."""
    try:
        import language_tool_python  # type: ignore
    except ImportError:
        raise RuntimeError(
            "The 'language-tool-python' package is not installed in the venv. "
            "Please run 'uv pip install language-tool-python' or equivalent in your configured virtual environment."
        )

    with _LT_CACHE_LOCK:
        tool = _LT_CACHE.get(bcp47_clean)
        if tool is not None:
            return tool
        try:
            # Starts local Java server (or queries existing one)
            tool = language_tool_python.LanguageTool(bcp47_clean)
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialize LanguageTool server for locale {bcp47_clean}. "
                f"Ensure that Java (JRE) is installed and available in the system PATH. Error: {e}"
            )
        _LT_CACHE[bcp47_clean] = tool
        return tool


def run_languagetool_check(text: str, bcp47: str) -> dict[str, Any]:
    """Execute grammar check on text using language_tool_python in the venv."""
    # Normalize locale code
    bcp47_clean = bcp47.replace("_", "-")
    tool = _cached_languagetool(bcp47_clean)

    try:
        matches = tool.check(text)
        errors = []
        for m in matches:
            # Replicate standard schema expected by WriterAgent grammar underlines
            errors.append({
                "wrong": _match_wrong_text(m),
                "correct": m.replacements[0] if m.replacements else "",
                "n_error_start": m.offset,
                "n_error_length": m.error_length,
                "short_comment": m.message,
                "full_comment": m.sentence,
                "rule_identifier": make_rule_identifier(LANGUAGETOOL_RULE_PREFIX, m.rule_id),
                "suggestions": m.replacements[:5],
                "reason": m.message,
                "type": "LanguageTool",
            })
        return {"errors": errors}
    except Exception as e:
        raise RuntimeError(f"LanguageTool check failed: {e}")
