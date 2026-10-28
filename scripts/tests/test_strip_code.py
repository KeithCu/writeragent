# WriterAgent tests — AST-based release stripping
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ast
import os

from scripts.strip_code import (
    should_skip_log_strip,
    should_skip_print_strip,
    should_skip_strip,
    strip_assert_statements,
    strip_deal_decorators,
    strip_grammar_obs_calls,
    strip_log_debug_info_calls,
    strip_print_calls,
    strip_production_code,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_should_skip_strip() -> None:
    assert should_skip_strip("plugin/testing_runner.py") is True
    assert should_skip_strip("plugin/tests/test_foo.py") is True
    assert should_skip_strip("tests/test_bar.py") is True
    assert should_skip_strip("plugin/framework/config.py") is False
    assert should_skip_strip("plugin/main.py") is False


def test_should_skip_print_strip_keep_list() -> None:
    assert should_skip_print_strip("plugin/framework/logging.py") is True
    assert should_skip_print_strip("plugin/scripting/venv_diagnostics.py") is True
    assert should_skip_print_strip("plugin/contrib/smolagents/monitoring.py") is True
    assert should_skip_print_strip("plugin/testing_runner.py") is True
    assert should_skip_print_strip("compute_service/config.py") is True
    assert should_skip_print_strip("plugin/main.py") is False
    assert should_skip_print_strip("plugin/chatbot/panel.py") is False


def test_should_skip_log_strip_keep_list() -> None:
    assert should_skip_log_strip("compute_service/server.py") is True
    assert should_skip_log_strip("plugin/testing_runner.py") is True
    assert should_skip_log_strip("plugin/main.py") is False
    assert should_skip_log_strip("plugin/framework/config.py") is False


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
    """grammar_obs-only strip leaves log.debug; production strip removes it."""
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


def test_strip_log_debug_info_removes_levels_keeps_warning_error(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "import logging\n"
        "log = logging.getLogger('t')\n"
        "logger = log\n"
        "\n"
        "def hello(self=None):\n"
        "    log.debug('d')\n"
        "    log.info('i')\n"
        "    logger.debug('ld')\n"
        "    logger.info('li')\n"
        "    logging.getLogger('x').debug('gd')\n"
        "    logging.getLogger('x').info('gi')\n"
        "    log.warning('w')\n"
        "    log.error('e')\n"
        "    log.exception('x')\n"
        "    print('keep until print strip')\n"
        "    return 1\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_log_debug_info_calls(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "log.debug" not in stripped
    assert "log.info" not in stripped
    assert "logger.debug" not in stripped
    assert "logger.info" not in stripped
    assert ".debug(" not in stripped
    assert ".info(" not in stripped
    assert "log.warning('w')" in stripped
    assert "log.error('e')" in stripped
    assert "log.exception('x')" in stripped
    assert "print('keep until print strip')" in stripped
    assert "return 1" in stripped


def test_strip_log_debug_info_sole_except_gets_pass(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "try:\n"
        "    x = 1\n"
        "except Exception:\n"
        "    log.debug('only', exc_info=True)\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_log_debug_info_calls(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "pass" in stripped
    assert "log.debug" not in stripped


def test_strip_print_removes_calls(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "def hello():\n"
        "    print('hello world')\n"
        "    pprint(obj)\n"
        "    log.warning('keep')\n"
        "    return 1\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_print_calls(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "print(" not in stripped
    assert "pprint(" not in stripped
    assert "log.warning('keep')" in stripped
    assert "return 1" in stripped


def test_strip_print_keeps_logging_fallback_path(tmp_path: Path) -> None:
    """Keep-list: plugin/framework/logging.py prints survive print strip."""
    os.makedirs(tmp_path / "plugin" / "framework", exist_ok=True)
    test_file = tmp_path / "plugin" / "framework" / "logging.py"
    original_code = (
        "import sys\n"
        "\n"
        "def init_logging():\n"
        "    print('WriterAgent: init_logging failed', file=sys.stderr)\n"
        "    log.debug('still stripped by log strip')\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_print_calls(str(tmp_path), dry_run=False)
    after_print = test_file.read_text(encoding="utf-8")
    assert "print('WriterAgent: init_logging failed'" in after_print

    strip_log_debug_info_calls(str(tmp_path), dry_run=False)
    after_log = test_file.read_text(encoding="utf-8")
    assert "print('WriterAgent: init_logging failed'" in after_log
    assert "log.debug" not in after_log


def test_strip_production_code_strips_debug_info_and_print(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "def f():\n"
        "    print('p')\n"
        "    log.debug('d')\n"
        "    log.info('i')\n"
        "    log.warning('w')\n"
        "    grammar_obs('g')\n"
        "    return 1\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_production_code(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "print(" not in stripped
    assert "log.debug" not in stripped
    assert "log.info" not in stripped
    assert "grammar_obs(" not in stripped
    assert "log.warning('w')" in stripped
    assert "return 1" in stripped


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


def test_strip_deal_decorators_multiline_keeps_shim_import(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_errors.py"
    original_code = (
        "from plugin.framework.deal_shim import deal\n"
        "\n"
        "@decorator_to_keep\n"
        "@deal.post(lambda result: isinstance(result, dict) and result.get('status') == 'error')\n"
        "@deal.ensure(\n"
        "    lambda e, result: isinstance(e, Exception)\n"
        "    or result.get('code') == 'INTERNAL_ERROR'\n"
        ")\n"
        "@deal.ensure(lambda e, result: result.get('code') == getattr(e, 'code', None))\n"
        "def format_error_payload(e):\n"
        "    return {'status': 'error', 'code': 'INTERNAL_ERROR'}\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_deal_decorators(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "from plugin.framework.deal_shim import deal" in stripped
    assert "@deal." not in stripped
    assert "deal.post" not in stripped
    assert "deal.ensure" not in stripped
    assert "@decorator_to_keep" in stripped
    assert "def format_error_payload(e):" in stripped
    assert "return {'status': 'error', 'code': 'INTERNAL_ERROR'}" in stripped


def test_strip_deal_decorators_dry_run(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "from plugin.framework.deal_shim import deal\n"
        "\n"
        "@deal.post(lambda result: True)\n"
        "def f():\n"
        "    return 1\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_deal_decorators(str(tmp_path), dry_run=True)

    assert test_file.read_text(encoding="utf-8") == original_code


def test_strip_production_code_strips_deal(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "from plugin.framework.deal_shim import deal\n"
        "\n"
        "@deal.pre(lambda x: True)\n"
        "def f(x):\n"
        "    log.debug('d')\n"
        "    return x\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_production_code(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    ast.parse(stripped)
    assert "@deal." not in stripped
    assert "log.debug" not in stripped
    assert "from plugin.framework.deal_shim import deal" in stripped
    assert "def f(x):" in stripped
    assert "return x" in stripped


def test_replace_thread_guard_implementation(tmp_path: Path) -> None:
    """thread_guard.py is overwritten with minimal no-op stubs."""
    from scripts.strip_code import replace_thread_guard_implementation
    os.makedirs(tmp_path / "plugin" / "framework", exist_ok=True)
    tg_file = tmp_path / "plugin" / "framework" / "thread_guard.py"
    tg_file.write_text("def assert_main_thread(what):\n    raise RuntimeError('heavy guard')\n", encoding="utf-8")

    replace_thread_guard_implementation(str(tmp_path), dry_run=False)

    stubbed = tg_file.read_text(encoding="utf-8")
    assert "GUARD_ON = False" in stubbed
    assert "def assert_main_thread(what: str) -> None:" in stubbed
    assert "def guard_uno(obj: Any) -> Any:" in stubbed
    assert "def background(fn: Any) -> Any:" in stubbed
    assert "def sync_host_dispatch()" in stubbed
    assert "def in_sync_host_dispatch() -> bool:" in stubbed
    assert "raise RuntimeError" not in stubbed
    assert "THREAD_VIOLATION_MARK" in stubbed

    # Verify stub executes cleanly and provides functional sync_host_dispatch
    import plugin.framework.thread_guard as tg

    ns: dict = {}
    exec(stubbed, ns)
    assert ns["in_sync_host_dispatch"]() is False
    with ns["sync_host_dispatch"]():
        assert ns["in_sync_host_dispatch"]() is True
    assert ns["in_sync_host_dispatch"]() is False
    assert ns["THREAD_VIOLATION_MARK"] == tg.THREAD_VIOLATION_MARK
    assert ns["__all__"] == tg.__all__


def test_omit_sidebar_test_hooks_deletes_file(tmp_path: Path) -> None:
    """Release trees must not ship a stub of press_send / set_query_text."""
    from scripts.strip_code import omit_sidebar_test_hooks

    dest = tmp_path / "plugin" / "chatbot"
    dest.mkdir(parents=True)
    target = dest / "sidebar_test_hooks.py"
    target.write_text("def press_send(*, listener=None):\n    return 1\n", encoding="utf-8")

    omit_sidebar_test_hooks(str(tmp_path), dry_run=False)

    assert not target.exists()


def test_strip_code_excludes_plugin_lib(tmp_path: Path) -> None:
    lib_file = tmp_path / "plugin" / "lib" / "somepkg" / "module.py"
    lib_file.parent.mkdir(parents=True)
    lib_file.write_text("def f():\n    log.debug('vendored')\n    return 1\n", encoding="utf-8")
    strip_production_code(str(tmp_path), dry_run=False)
    content = lib_file.read_text(encoding="utf-8")
    assert "log.debug('vendored')" in content


def test_strip_code_fails_on_syntax_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "plugin" / "bad.py"
    bad_file.parent.mkdir(parents=True)
    bad_file.write_text("def broken(:\n", encoding="utf-8")
    import pytest
    with pytest.raises(RuntimeError) as exc_info:
        strip_production_code(str(tmp_path), dry_run=False)
    assert "Failed stripping" in str(exc_info.value)
    assert "plugin/bad.py" in str(exc_info.value)


def test_strip_assert_removes_statements_and_compiles(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "assert True\n"
        "\n"
        "def check(x):\n"
        "    assert x > 0, 'must be positive'\n"
        "    y = x * 2\n"
        "    assert y > x\n"
        "    assert (\n"
        "        y % 2 == 0\n"
        "    ), 'must be even'\n"
        "    assert 1; z = y + 1\n"
        "    return z\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_assert_statements(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    tree = ast.parse(stripped)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    compiled = compile(stripped, str(test_file), "exec")
    ns: dict[str, object] = {}
    exec(compiled, ns)
    check_fn = ns["check"]
    assert callable(check_fn)
    # Even with x = -5 (which would have failed assert x > 0), the function succeeds because asserts were stripped
    assert check_fn(-5) == -9


def test_strip_assert_keeps_pass_when_needed(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "def empty_func():\n"
        "    assert 1 == 1\n"
        "\n"
        "def branch(x):\n"
        "    if x:\n"
        "        assert x > 0\n"
        "    else:\n"
        "        assert x <= 0\n"
        "    try:\n"
        "        assert x != 42\n"
        "    except Exception:\n"
        "        assert False\n"
        "    if not x: assert True\n"
        "    while False:\n"
        "        assert False\n"
        "        assert 0\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_assert_statements(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    tree = ast.parse(stripped)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    compiled = compile(stripped, str(test_file), "exec")
    ns: dict[str, object] = {}
    exec(compiled, ns)
    ns["empty_func"]()
    ns["branch"](10)
    ns["branch"](0)


def test_strip_assert_dry_run(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = "def f():\n    assert True\n    return 1\n"
    test_file.write_text(original_code, encoding="utf-8")

    strip_assert_statements(str(tmp_path), dry_run=True)

    assert test_file.read_text(encoding="utf-8") == original_code


def test_strip_production_code_strips_asserts(tmp_path: Path) -> None:
    test_file = tmp_path / "mock_file.py"
    original_code = (
        "from plugin.framework.deal_shim import deal\n"
        "\n"
        "@deal.pre(lambda x: x > 0)\n"
        "def f(x):\n"
        "    assert x > 0\n"
        "    print('logging value', x)\n"
        "    log.debug('debug value: %s', x)\n"
        "    return x * 10\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_production_code(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    tree = ast.parse(stripped)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    assert "@deal.pre" not in stripped
    assert "print(" not in stripped
    assert "log.debug" not in stripped
    compiled = compile(stripped, str(test_file), "exec")
    ns: dict[str, object] = {}
    exec(compiled, ns)
    assert ns["f"](5) == 50


def test_strip_production_code_compute_service_preserves_log_and_print(tmp_path: Path) -> None:
    cs_dir = tmp_path / "compute_service"
    cs_dir.mkdir(parents=True)
    test_file = cs_dir / "server.py"
    original_code = (
        "from plugin.framework.deal_shim import deal\n"
        "\n"
        "@deal.pre(lambda x: x > 0)\n"
        "def start_server(x):\n"
        "    assert x > 0\n"
        "    print('Starting CLI error fallback:', x)\n"
        "    log.info('Starting Python Compute Service on port %d', x)\n"
        "    return x\n"
    )
    test_file.write_text(original_code, encoding="utf-8")

    strip_production_code(str(tmp_path), dry_run=False)

    stripped = test_file.read_text(encoding="utf-8")
    tree = ast.parse(stripped)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    assert "@deal.pre" not in stripped
    assert "print(" in stripped
    assert "log.info(" in stripped

