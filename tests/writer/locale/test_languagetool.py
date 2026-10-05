# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LanguageTool venv helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys
import threading
import time

from plugin.writer.locale.grammar_ignore_rules import LANGUAGETOOL_RULE_PREFIX, make_rule_identifier
from plugin.writer.locale.languagetool import _match_wrong_text, run_languagetool_check


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
        patch("plugin.writer.locale.languagetool._LT_CACHE", {"en-US": tool}),
    ):
        res = run_languagetool_check("teh word", "en-US")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["rule_identifier"] == make_rule_identifier(LANGUAGETOOL_RULE_PREFIX, "MORFOLOGIK_RULE_EN_US")
    assert err["type"] == "LanguageTool"
    assert err["wrong"] == "teh"


def test_empty_matched_text_is_not_replaced_by_lenient_slice() -> None:
    """``matched_text=""`` must not fall through to an out-of-range context slice."""
    negative = SimpleNamespace(
        matched_text="",
        context="hello world",
        offset_in_context=-1,
        error_length=5,
    )
    assert _match_wrong_text(negative) == ""

    truncated = SimpleNamespace(
        matched_text="",
        context="hello",
        offset_in_context=1,
        error_length=100,
    )
    assert _match_wrong_text(truncated) == ""

    missing = SimpleNamespace(context="hello world", offset_in_context=0, error_length=5)
    assert _match_wrong_text(missing) == "hello"

    preferred = SimpleNamespace(
        matched_text="teh",
        context="xxxx",
        offset_in_context=0,
        error_length=4,
    )
    assert _match_wrong_text(preferred) == "teh"


def test_lt_cache_constructs_one_client_under_contention() -> None:
    """Two threads missing the cache must not each start a LanguageTool JVM."""
    constructed = {"n": 0}
    entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()

    class FakeLanguageTool:
        def __init__(self, lang: str) -> None:
            del lang
            with count_lock:
                constructed["n"] += 1
                n = constructed["n"]
            if n == 1:
                entered.set()
                assert release.wait(2.0)
            self.check = MagicMock(return_value=[])

    fake_mod = MagicMock()
    fake_mod.LanguageTool = FakeLanguageTool
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            run_languagetool_check("hello", "en-US")
        except BaseException as exc:
            errors.append(exc)

    with patch.dict(sys.modules, {"language_tool_python": fake_mod}), patch(
        "plugin.writer.locale.languagetool._LT_CACHE", {}
    ):
        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        assert entered.wait(2.0)
        second.start()
        time.sleep(0.05)
        # First thread is inside LanguageTool() holding the cache lock, so the
        # second must still be waiting rather than constructing its own JVM.
        assert constructed["n"] == 1
        assert second.is_alive()
        release.set()
        first.join(2.0)
        second.join(2.0)
    assert errors == []
    assert constructed["n"] == 1
    assert not first.is_alive()
    assert not second.is_alive()
