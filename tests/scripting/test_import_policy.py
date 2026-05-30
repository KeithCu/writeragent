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
