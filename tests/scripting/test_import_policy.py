# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.import_policy import (
    format_venv_import_policy_for_prompt,
)
from plugin.scripting.sandbox import (
    build_sandbox_policy,
    policy_blocked_imports,
)
import pytest


@pytest.fixture(autouse=True)
def _register_prompt_hooks_for_import_policy_tests():
    from plugin.writeragent_hooks import register_writeragent_prompt_hooks

    register_writeragent_prompt_hooks()


def test_blocked_list_excludes_safe_stdlib_and_packages():
    blocked = tuple(sorted(policy_blocked_imports(build_sandbox_policy())))
    for safe in (
        "json",
        "numpy",
        "matplotlib",
        "io",
        "logging",
        "glob",
        "zipfile",
        "gzip",
        "bz2",
        "lzma",
        "asyncio",
        "inspect",
        "sys",
        "signal",
        "concurrent.futures",
        "pathlib",
    ):
        assert safe not in blocked
    for dangerous in ("subprocess", "importlib", "pickle"):
        assert dangerous in blocked


def test_network_blocked_unless_enabled():
    from plugin.scripting.sandbox import (
        build_sandbox_policy,
        import_authorized,
        policy_allowlist,
        policy_blocked_imports,
    )

    assert "requests" in tuple(sorted(policy_blocked_imports(build_sandbox_policy())))
    network_policy = build_sandbox_policy(network=True)
    assert "requests" not in policy_blocked_imports(network_policy)
    assert import_authorized("requests", policy_allowlist(network_policy)) is True


def test_venv_blocked_includes_os_not_sys():
    blocked = tuple(sorted(policy_blocked_imports(build_sandbox_policy())))
    for b in ("requests", "subprocess", "importlib", "pickle"):
        assert b in blocked
    assert "os" not in blocked
    assert "sys" not in blocked


def test_venv_policy_auto_imports_match_constants():
    compact = format_venv_import_policy_for_prompt(compact=True)
    full = format_venv_import_policy_for_prompt(compact=False)
    for module_name, import_stmt in AUTO_IMPORTS.items():
        marker = " as "
        alias = import_stmt.rsplit(marker, 1)[-1].strip() if marker in import_stmt else module_name
        for policy in (compact, full):
            assert alias in policy
            assert module_name in policy


def test_venv_policy_has_no_cross_app_plot_branches():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert "image_insert" not in policy
    assert "PLOTS:" not in policy

def test_venv_policy_does_not_advertise_duckdb():
    """LLM-facing =PY / Calc policy blurbs must not steer agents toward DuckDB."""
    from plugin.framework.prompts import (
        CALC_WORKFLOW,
        python_specialized_sub_agent_hint,
    )

    compact = format_venv_import_policy_for_prompt(compact=True)
    full = format_venv_import_policy_for_prompt(compact=False)
    from plugin.framework.prompts import CALC_FORMULA_SYNTAX_TEMPLATE
    from plugin.scripting.import_policy import python_venv_auto_imports_note

    calc_syntax = CALC_FORMULA_SYNTAX_TEMPLATE.replace("{venv_policy}", compact)
    surfaces = (
        compact,
        full,
        calc_syntax,
        python_venv_auto_imports_note(),
        CALC_WORKFLOW,
        python_specialized_sub_agent_hint("Calc"),
        python_specialized_sub_agent_hint("Writer"),
    )
    for text in surfaces:
        lower = text.lower()
        assert "query_folder_sql" not in lower
        assert "session_duckdb" not in lower
        assert "run_sql" not in lower
        assert "duckdb" not in lower
    assert "duckdb" not in tuple(sorted(policy_blocked_imports(build_sandbox_policy())))


def test_blocked_categories_are_disjoint_and_valid() -> None:
    from plugin.scripting.sandbox import DANGEROUS_MODULES, FILE_BLOCKED_MODULES, NETWORK_BLOCKED_MODULES

    assert len(DANGEROUS_MODULES) > 0
    assert len(NETWORK_BLOCKED_MODULES) > 0
    assert len(FILE_BLOCKED_MODULES) > 0

    dangerous = set(DANGEROUS_MODULES)
    network = set(NETWORK_BLOCKED_MODULES)
    files = set(FILE_BLOCKED_MODULES)

    assert len(dangerous) == len(DANGEROUS_MODULES)
    assert len(network) == len(NETWORK_BLOCKED_MODULES)
    assert len(files) == len(FILE_BLOCKED_MODULES)

    assert dangerous.isdisjoint(network)
    assert dangerous.isdisjoint(files)
    assert network.isdisjoint(files)

    for category in (DANGEROUS_MODULES, NETWORK_BLOCKED_MODULES, FILE_BLOCKED_MODULES):
        for entry in category:
            assert not entry.startswith(".")
            assert not entry.endswith(".*")
            assert "*" not in entry
            parts = entry.split(".")
            assert all(part.isidentifier() for part in parts)


def test_compact_policy_lists_network_block_without_string_prepend() -> None:
    from plugin.scripting.sandbox import NETWORK_BLOCKED_MODULES

    policy = format_venv_import_policy_for_prompt(compact=True)
    assert ", ".join(NETWORK_BLOCKED_MODULES) in policy


def test_blocked_entries_are_valid_import_roots():
    from plugin.scripting.sandbox import build_sandbox_policy, policy_blocked_imports

    blocked = policy_blocked_imports(build_sandbox_policy())
    assert len(blocked) > 0
    for entry in blocked:
        assert "*" not in entry
        parts = entry.split(".")
        assert all(part.isidentifier() for part in parts)
    assert "subprocess" in blocked
    assert "socket" in blocked
    assert "os" not in blocked


def test_import_failure_messages_short_and_hide_blocklist():
    """Import failure messages must not dump the whole blocklist or expose duckdb."""
    import ast
    from plugin.scripting.sandbox import SANDBOX_BLOCKED_MODULES
    from plugin.scripting.sandbox_cache import validate_sandbox_ast

    allowed = ["math", "duckdb", "duckdb.*"]

    token = SANDBOX_BLOCKED_MODULES.set(frozenset({"os"}))
    try:
        err_import = validate_sandbox_ast(ast.parse("import os"), allowed)
        assert err_import == "Import of os is not allowed."
        assert "duckdb" not in err_import
        assert "Authorized imports" not in err_import

        err_from = validate_sandbox_ast(ast.parse("from os import path"), allowed)
        assert err_from == "Import from os is not allowed."
        assert "duckdb" not in err_from
        assert "Authorized imports" not in err_from
    finally:
        SANDBOX_BLOCKED_MODULES.reset(token)

    err_rel = validate_sandbox_ast(ast.parse("from . import helper"), allowed)
    assert err_rel == "Relative imports are not allowed."

    err_rel_dotted = validate_sandbox_ast(ast.parse("from .numpy import foo"), allowed)
    assert err_rel_dotted == "Relative imports are not allowed."


def test_venv_policy_omits_internal_writeragent_modules() -> None:
    from plugin.scripting.sandbox import import_authorized

    policy = format_venv_import_policy_for_prompt(compact=False)
    for internal in (
        "writeragent.framework",
        "payload_codec",
        "writeragent_api",
        "writeragent_namespace",
        "plugin.scripting.analysis",
    ):
        assert internal not in policy

    assert import_authorized("writeragent.scripting.analysis", ()) is True
    assert import_authorized("writeragent.framework.config", ()) is False


def test_extra_modules_override_every_blocked_entry() -> None:
    from plugin.scripting.sandbox import build_sandbox_policy, import_authorized, policy_blocked_imports

    for e in policy_blocked_imports(build_sandbox_policy()):
        assert import_authorized(e, ()) is False
        assert import_authorized(e, (e,)) is True
        assert import_authorized(e + ".child", (e,)) is True


