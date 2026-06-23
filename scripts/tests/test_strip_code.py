# WriterAgent tests — AST-based grammar_obs stripping
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ast
from pathlib import Path

from scripts.strip_code import should_skip_strip, strip_grammar_obs_calls, strip_production_code


def test_should_skip_strip() -> None:
    assert should_skip_strip("plugin/testing_runner.py") is True
    assert should_skip_strip("plugin/tests/test_foo.py") is True
    assert should_skip_strip("tests/test_bar.py") is True
    assert should_skip_strip("plugin/framework/config.py") is False
    assert should_skip_strip("plugin/main.py") is False


def test_strip_grammar_obs_removes_calls_keeps_imports(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "from .grammar_obs import grammar_obs, emit_grammar_status\n"
        "\n"
        "def hello():\n"
        "    print('hello world')\n"
        "    log.debug('debugging trace')\n"
        "    grammar_obs('observation')\n"
        "    _grammar_obs('internal observation')\n"
        "    grammar_obs(\n"
        "        'multi',\n"
        "        a=1,\n"
        "    )\n"
        "    return 42\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_grammar_obs_calls(str(tmp_path), dry_run=False)

    stripped_code = test_file.read_text(encoding="utf-8")
    ast.parse(stripped_code)
    assert "from .grammar_obs import grammar_obs" in stripped_code
    assert "print('hello world')" in stripped_code
    assert "log.debug" in stripped_code
    assert "grammar_obs(" not in stripped_code
    assert "_grammar_obs(" not in stripped_code
    assert "return 42" in stripped_code


def test_strip_production_code_alias(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    test_file.write_text("def f():\n    grammar_obs('x')\n    return 1\n", encoding="utf-8")
    strip_production_code(str(tmp_path), dry_run=False)
    assert "grammar_obs(" not in test_file.read_text(encoding="utf-8")
    assert "return 1" in test_file.read_text(encoding="utf-8")


def test_strip_grammar_obs_keeps_pass_when_needed(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "if True:\n"
        "    grammar_obs('only stmt')\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_grammar_obs_calls(str(tmp_path), dry_run=False)

    stripped_code = test_file.read_text(encoding="utf-8")
    assert "pass" in stripped_code
    ast.parse(stripped_code)


def test_strip_grammar_obs_dry_run(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = "def hello():\n    grammar_obs('x')\n    return 42\n"
    test_file.write_text(original_code, encoding="utf-8")

    strip_grammar_obs_calls(str(tmp_path), dry_run=True)

    assert test_file.read_text(encoding="utf-8") == original_code


def test_strip_multiline_grammar_obs_no_dangling_first_line(tmp_path: Path) -> None:
    """Regression: old stripper used range(start, end) with 1-based line numbers as indices.

    That deleted continuation lines but could leave the opening ``grammar_obs(`` line,
    producing invalid Python. The fix uses 0-based ``first_idx``..``last_idx`` inclusive.
    """
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "def covered_span():\n"
        "    before = 1\n"
        "    grammar_obs(\n"
        '        "do_proofreading_covered_span",\n'
        '        doc_id="doc1",\n'
        "        grammar_bcp47=\"en-US\",\n"
        "        covered_end=42,\n"
        "        sentence_count=2,\n"
        "    )\n"
        "    after = 2\n"
        "    return after\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_grammar_obs_calls(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "grammar_obs(" not in stripped
    assert "do_proofreading_covered_span" not in stripped
    assert "before = 1" in stripped
    assert "after = 2" in stripped
    assert "return after" in stripped
    lines = [ln.strip() for ln in stripped.splitlines()]
    assert not any(ln.startswith("grammar_obs") for ln in lines)


def test_strip_grammar_obs_module_unchanged(tmp_path: Path) -> None:
    """The grammar_obs definition module keeps its log.debug body."""
    test_file = tmp_path / "grammar_obs.py"
    original_code = (
        "import logging\n"
        "log = logging.getLogger('writeragent.grammar')\n"
        "\n"
        "def grammar_obs(event: str, **fields):\n"
        "    log.debug('[grammar] obs %s', event)\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_grammar_obs_calls(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    assert "def grammar_obs" in stripped
    assert "log.debug" in stripped


def test_strip_main_thread_only_decorators(tmp_path: Path) -> None:
    """decorators with main_thread_only are stripped successfully."""
    from scripts.strip_code import strip_main_thread_only_decorators
    test_file = tmp_path / "mock_context.py"
    original_code = (
        "from plugin.framework.thread_guard import main_thread_only\n"
        "\n"
        "@main_thread_only\n"
        "def get_desktop():\n"
        "    return 'desktop'\n"
        "\n"
        "@decorator_to_keep\n"
        "@main_thread_only\n"
        "async def get_active_document():\n"
        "    return 'doc'\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_main_thread_only_decorators(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "@main_thread_only" not in stripped
    assert "@decorator_to_keep" in stripped
    assert "def get_desktop():" in stripped
    assert "async def get_active_document():" in stripped


def test_replace_thread_guard_implementation(tmp_path: Path) -> None:
    """thread_guard.py is overwritten with minimal no-op stubs."""
    import os
    from scripts.strip_code import replace_thread_guard_implementation
    os.makedirs(tmp_path / "plugin" / "framework", exist_ok=True)
    tg_file = tmp_path / "plugin" / "framework" / "thread_guard.py"
    tg_file.write_text("def assert_main_thread(what):\n    raise RuntimeError('heavy guard')\n", encoding="utf-8")

    replace_thread_guard_implementation(str(tmp_path), dry_run=False)

    stubbed = tg_file.read_text(encoding="utf-8")
    assert "GUARD_ON = False" in stubbed
    assert "def assert_main_thread(what: str) -> None:" in stubbed
    assert "raise RuntimeError" not in stubbed

