# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LanguageTool venv helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import sys

from plugin.writer.locale.grammar_ignore_rules import LANGUAGETOOL_RULE_PREFIX, make_rule_identifier
from plugin.scripting.venv.languagetool import run_languagetool_check


def test_run_languagetool_check_prefixes_rule_identifier() -> None:
    match = MagicMock()
    match.matched_text = "teh"
    match.context = "teh word"
    match.offset_in_context = 0
    match.error_length = 3
    match.message = "Possible spelling mistake"
    match.sentence = "teh word"
    match.rule_id = "MORFOLOGIK_RULE_EN_US"
    match.replacements = ["the"]

    tool = MagicMock()
    tool.check.return_value = [match]

    with (
        patch.dict(sys.modules, {"language_tool_python": MagicMock()}),
        patch("plugin.scripting.venv.languagetool._LT_CACHE", {"en-US": tool}),
    ):
        res = run_languagetool_check("teh word", "en-US")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["rule_identifier"] == make_rule_identifier(LANGUAGETOOL_RULE_PREFIX, "MORFOLOGIK_RULE_EN_US")
    assert err["type"] == "LanguageTool"
