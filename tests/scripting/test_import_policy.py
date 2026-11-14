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
    format_matplotlib_plot_hint,
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


def test_venv_policy_auto_imports_match_constants():
    compact = format_venv_import_policy_for_prompt(compact=True)
    full = format_venv_import_policy_for_prompt(compact=False)
    for module_name, import_stmt in AUTO_IMPORTS.items():
        marker = " as "
        alias = import_stmt.rsplit(marker, 1)[-1].strip() if marker in import_stmt else module_name
        for policy in (compact, full):
            assert alias in policy
            assert module_name in policy


def test_matplotlib_plot_hint_impress_matches_draw():
    assert format_matplotlib_plot_hint(doc_type="impress") == format_matplotlib_plot_hint(doc_type="draw")


def test_matplotlib_plot_hint_unknown_empty():
    assert format_matplotlib_plot_hint(doc_type="unknown") == ""


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


def test_compact_policy_lists_network_block_without_string_prepend() -> None:
    from plugin.scripting.sandbox import NETWORK_BLOCKED_MODULES

    policy = format_venv_import_policy_for_prompt(compact=True)
    assert ", ".join(NETWORK_BLOCKED_MODULES) in policy


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


