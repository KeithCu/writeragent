# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

from plugin.framework.constants import (
    PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE,
    PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE,
    python_specialized_sub_agent_hint,
)
from plugin.scripting.import_policy import (
    PYTHON_VENV_SANDBOX_CONTEXT_PREFIX,
    format_inprocess_import_policy_for_prompt,
    format_matplotlib_plot_hint,
    format_units_helper_hint,
    format_venv_import_policy_for_prompt,
    inprocess_authorized_modules,
    venv_authorized_top_level_modules,
    venv_blocked_modules,
)


def test_venv_authorized_includes_json_numpy_matplotlib():
    allowed = venv_authorized_top_level_modules()
    assert "json" in allowed
    assert "numpy" in allowed
    assert "matplotlib" in allowed
    assert "duckdb" in allowed


def test_venv_authorized_excludes_requests():
    assert "requests" not in venv_authorized_top_level_modules()


def test_venv_blocked_includes_sys_os():
    blocked = venv_blocked_modules()
    assert "sys" in blocked
    assert "os" in blocked
    assert "requests" in blocked


def test_sandbox_prefix_uses_sandbox_twice():
    assert PYTHON_VENV_SANDBOX_CONTEXT_PREFIX.lower().count("sandbox") >= 2


def test_venv_policy_prefix_before_module_lists():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert policy.startswith("PYTHON VENV SANDBOX:")
    assert policy.index("Allowed stdlib") > policy.index("This sandbox")


def test_venv_policy_compact_mentions_blocked_categories():
    policy = format_venv_import_policy_for_prompt(compact=True)
    assert "no networking" in policy
    assert "host escape" in policy
    assert "DO NOT import numpy" in policy
    assert "xl" in policy


def test_inprocess_policy_is_stdlib_focused():
    policy = format_inprocess_import_policy_for_prompt()
    assert "IN-PROCESS SANDBOX" in policy
    assert "json" in policy
    assert "numpy" not in inprocess_authorized_modules()


def test_python_specialized_sub_agent_hint_includes_full_policy():
    hint = python_specialized_sub_agent_hint("Writer")
    assert "Allowed stdlib in this sandbox" in hint
    assert "does not inject spreadsheet" in hint


def test_tool_note_includes_sandbox_prefix():
    assert PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE.startswith("PYTHON VENV SANDBOX:")
    assert PYTHON_VENV_AUTO_IMPORTS_PROMPT_LINE in PYTHON_VENV_AUTO_IMPORTS_TOOL_NOTE


def test_matplotlib_plot_hint_calc():
    hint = format_matplotlib_plot_hint(doc_type="calc")
    assert "Do not call insert_image" in hint
    assert "data_range" in hint
    assert "active sheet" in hint


def test_matplotlib_plot_hint_writer():
    hint = format_matplotlib_plot_hint(doc_type="writer")
    assert "insert_image" in hint
    assert "image_path" in hint


def test_matplotlib_plot_hint_draw():
    hint = format_matplotlib_plot_hint(agent_label="Draw")
    assert "insert_image" in hint
    assert "slide" in hint.lower() or "page" in hint.lower()


def test_matplotlib_plot_hint_unknown_empty():
    assert format_matplotlib_plot_hint(doc_type="unknown") == ""


def test_venv_policy_has_no_cross_app_plot_branches():
    policy = format_venv_import_policy_for_prompt(compact=False)
    assert "insert_image" not in policy
    assert "PLOTS:" not in policy


def test_python_specialized_sub_agent_calc_plot_hint():
    hint = python_specialized_sub_agent_hint("Calc")
    assert "PLOTS:" in hint
    assert "Do not call insert_image" in hint


def test_format_units_helper_hint():
    hint = format_units_helper_hint()
    assert "Units Helpers" in hint
    assert "run_units" in hint


def test_python_specialized_sub_agent_units_hint():
    hint = python_specialized_sub_agent_hint("Writer")
    assert "Units Helpers" in hint
