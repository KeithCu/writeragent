# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

from plugin.framework.prompts import (
    python_specialized_sub_agent_hint,
)
from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.import_policy import (
    PYTHON_VENV_SANDBOX_CONTEXT_PREFIX,
    format_matplotlib_plot_hint,
    format_units_helper_hint,
    format_venv_import_policy_for_prompt,
    venv_blocked_modules,
)
import pytest


def test_blocked_list_excludes_safe_stdlib_and_packages():
    blocked = venv_blocked_modules()
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

    assert "requests" in venv_blocked_modules()
    network_policy = build_sandbox_policy(network=True)
    assert "requests" not in policy_blocked_imports(network_policy)
    assert import_authorized("requests", policy_allowlist(network_policy)) is True


def test_venv_blocked_includes_os_not_sys():
    blocked = venv_blocked_modules()
    for b in ("requests", "subprocess", "importlib", "pickle"):
        assert b in blocked
    assert "os" not in blocked
    assert "sys" not in blocked


def test_sandbox_prefix_uses_sandbox_twice():
    assert PYTHON_VENV_SANDBOX_CONTEXT_PREFIX.lower().count("sandbox") >= 2


def test_venv_policy_prefix_before_blocked_lists():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert policy.startswith("PYTHON VENV SANDBOX:")
    assert "Blocked host escape" in policy
    assert policy.index("Blocked host escape") > policy.index("PYTHON VENV SANDBOX")
    assert "Allowed stdlib" not in policy
    assert "Allowed packages" not in policy


def test_venv_policy_compact_mentions_blocked_categories():
    policy = format_venv_import_policy_for_prompt(compact=True)
    assert "networking" in policy
    assert "host escape" in policy
    assert "file writes" in policy
    assert "DO NOT import numpy" in policy
    assert "calc" in policy
    assert "st" in policy
    assert "plt" in policy
    assert "dt" in policy


def test_venv_policy_preimported_lists_aliases():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert "Pre-imported" in policy
    for name in ("np", "pd", "sp", "st", "plt", "dt", "calc"):
        assert name in policy
    assert 'xl("%Pn%")' in policy
    assert "binding-only" in policy
    assert "scipy.stats" in policy
    assert "matplotlib.pyplot" in policy


def test_venv_policy_auto_imports_match_constants():
    from plugin.scripting.import_policy import _auto_import_alias

    compact = format_venv_import_policy_for_prompt(compact=True)
    full = format_venv_import_policy_for_prompt(compact=False)
    for module_name, import_stmt in AUTO_IMPORTS.items():
        alias = _auto_import_alias(module_name, import_stmt)
        for policy in (compact, full):
            assert alias in policy
            assert module_name in policy



@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param("Writer", "Blocked host escape", "does not inject spreadsheet", id="test_python_specialized_sub_agent_hint_includes_full_policy"),
        pytest.param("Calc", "PLOTS:", "Do not call image_insert", id="test_python_specialized_sub_agent_calc_plot_hint"),
    ],
)
def test_python_specialized_sub_agent_hint_includes_full_policy(value, value_2, value_3):
    hint = python_specialized_sub_agent_hint(value)
    assert value_2 in hint
    assert value_3 in hint


def test_tool_note_includes_sandbox_prefix():
    import plugin.framework.prompts as prompts

    prompts._ensure_venv_import_policy_strings()
    assert prompts.PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE.startswith("PYTHON VENV SANDBOX:")
    assert prompts.PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE in prompts.PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE


def test_matplotlib_plot_hint_calc():
    hint = format_matplotlib_plot_hint(doc_type="calc")
    assert "Do not call image_insert" in hint
    assert "data_range" in hint
    assert "active sheet" in hint


def test_matplotlib_plot_hint_writer():
    hint = format_matplotlib_plot_hint(doc_type="writer")
    assert "image_insert" in hint
    assert "image_path" in hint


def test_matplotlib_plot_hint_draw():
    hint = format_matplotlib_plot_hint(agent_label="Draw")
    assert "image_insert" in hint
    assert "slide" in hint.lower() or "page" in hint.lower()


def test_matplotlib_plot_hint_unknown_empty():
    assert format_matplotlib_plot_hint(doc_type="unknown") == ""


def test_venv_policy_has_no_cross_app_plot_branches():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert "image_insert" not in policy
    assert "PLOTS:" not in policy

def test_venv_policy_does_not_advertise_duckdb():
    """LLM-facing =PY / Calc policy blurbs must not steer agents toward DuckDB."""
    import plugin.framework.prompts as prompts
    from plugin.framework.prompts import (
        CALC_WORKFLOW,
        python_specialized_sub_agent_hint,
    )

    prompts._ensure_venv_import_policy_strings()
    compact = format_venv_import_policy_for_prompt(compact=True)
    full = format_venv_import_policy_for_prompt(compact=False)
    surfaces = (
        compact,
        full,
        prompts.CALC_FORMULA_SYNTAX,
        prompts.PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE,
        prompts.PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE,
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
    assert "duckdb" not in venv_blocked_modules()


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


def test_format_units_helper_hint():
    hint = format_units_helper_hint()
    assert "Units Helpers" in hint
    assert "run_units" in hint


def test_python_specialized_sub_agent_units_hint():
    hint = python_specialized_sub_agent_hint("Writer")
    assert "Units Helpers" in hint


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


def test_prompt_lists_only_blocked_modules():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert "Blocked host escape" in policy
    assert "Allowed packages" not in policy
    assert "plugin.scripting.analysis" not in policy
    assert "duckdb" not in policy.lower()


def test_import_failure_messages_short_and_hide_blocklist():
    """Import failure messages must not dump the whole blocklist or expose duckdb."""
    import ast
    from plugin.scripting.sandbox_cache import validate_sandbox_ast

    allowed = ["math", "duckdb", "duckdb.*"]

    err_import = validate_sandbox_ast(ast.parse("import os"), allowed, blocked_imports=["os"])
    assert err_import == "Import of os is not allowed."
    assert "duckdb" not in err_import
    assert "Authorized imports" not in err_import

    err_from = validate_sandbox_ast(ast.parse("from os import path"), allowed, blocked_imports=["os"])
    assert err_from == "Import from os is not allowed."
    assert "duckdb" not in err_from
    assert "Authorized imports" not in err_from

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


