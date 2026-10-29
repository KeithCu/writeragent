# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair (FQN) for tool schema normalize helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.tool import to_openai_schema, to_mcp_schema, ToolBase
from plugin.framework.tool_schema import _normalize_schema_for_strict_providers

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGET = "plugin.framework.tool_schema._normalize_schema_for_strict_providers"


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


_scalar = st.sampled_from(["string", "integer", "number", "boolean"])
_prop = st.fixed_dictionaries({"type": _scalar})
_schema = st.fixed_dictionaries(
    {
        "type": st.just("object"),
        "properties": st.dictionaries(st.sampled_from(["a", "b", "c"]), _prop, min_size=0, max_size=3),
        "required": st.lists(st.sampled_from(["a", "b", "c"]), max_size=3, unique=True),
    }
)


class _DummyTestTool(ToolBase):
    name = "dummy_test_tool"
    description = "A dummy tool for schema testing."
    parameters = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}


def test_to_openai_and_mcp_schema_structure() -> None:
    tool = _DummyTestTool()
    openai_s = to_openai_schema(tool)
    assert openai_s["type"] == "function"
    assert openai_s["function"]["name"] == "dummy_test_tool"
    assert openai_s["function"]["description"] == "A dummy tool for schema testing."
    assert "parameters" in openai_s["function"]

    mcp_s = to_mcp_schema(tool)
    assert mcp_s["name"] == "dummy_test_tool"
    assert mcp_s["description"] == "A dummy tool for schema testing."
    assert "inputSchema" in mcp_s
    assert "document_url" in mcp_s["inputSchema"]["properties"]


def test_empty_required_removed() -> None:
    out = _normalize_schema_for_strict_providers({"type": "object", "properties": {}, "required": []})
    assert isinstance(out, dict)
    assert "required" not in out


def test_optional_scalar_nullable() -> None:
    out = _normalize_schema_for_strict_providers(
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
    )
    assert out["properties"]["x"]["type"] == ["string", "null"]


@given(schema=_schema)
@settings(max_examples=60)
def test_hypothesis_normalize_idempotent(schema: dict) -> None:
    once = _normalize_schema_for_strict_providers(schema)
    twice = _normalize_schema_for_strict_providers(once)
    assert twice == once
    assert isinstance(once, dict)
    assert once.get("required") != []


@pytest.mark.slow
def test_crosshair_normalize_schema_fqn_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", _CROSSHAIR_TARGET],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
