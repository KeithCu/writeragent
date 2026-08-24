# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LO eval dispatch aliases (mocked get_tools().execute, no soffice)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
_REPO = Path(__file__).resolve().parents[2]
for _p in (_PO, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tools_lo as tl  # noqa: E402


def test_writer_html_export_unwraps_lo_headings_and_bold() -> None:
    raw = (
        '<h1 data-lo-style="Heading1"><a id="a__Introduction"><span/></a>Introduction</h1>'
        '<p><b>Developer</b></p>'
    )
    out = tl._compact_writer_html(raw)
    assert "<h1>Introduction</h1>" in out
    assert "<strong>" in out


def test_write_cell_range_aliases_to_write_formula_range() -> None:
    name, params = tl.normalize_lo_tool(
        "write_cell_range",
        {"range": "C2:C5", "values": [0.8, 0.4, 0.64, 1.0]},
        kind="calc",
    )
    assert name == "write_formula_range"
    assert params["range"] == ["C2:C5"]
    assert json.loads(params["values"]) == [0.8, 0.4, 0.64, 1.0]


def test_sort_range_name_to_index_and_used_range() -> None:
    name, params = tl.normalize_lo_tool(
        "sort_range",
        {"sort_column": "Revenue", "ascending": False},
        kind="calc",
        headers=["Product", "Revenue"],
        used_range="A1:B5",
    )
    assert name == "sort_range"
    assert params["sort_column"] == 1
    assert params["range"] == ["A1:B5"]
    assert params["ascending"] is False


def test_page_index_alias() -> None:
    name, params = tl.normalize_lo_tool(
        "get_draw_tree",
        {"page_index": 0},
        kind="draw",
    )
    assert name == "get_draw_tree"
    assert params["page"] == 0
    assert "page_index" not in params


def test_execute_impl_uses_bypass_thread_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_tools = MagicMock()
    mock_tools.execute.return_value = {"status": "ok", "tree": []}
    monkeypatch.setattr("plugin.main.get_tools", lambda: mock_tools)
    monkeypatch.setattr(tl.LOBackend, "acquire_document", classmethod(lambda cls, kind=None: object()))
    monkeypatch.setattr(tl.LOBackend, "current_kind", classmethod(lambda cls: "draw"))
    monkeypatch.setattr(tl, "_tool_ctx", lambda doc, kind: object())

    out = tl._execute_lo_tool_impl(
        "shape_upsert",
        {
            "action": "create",
            "shape_type": "ellipse",
            "text": "Start",
            "x": 1000,
            "y": 500,
            "width": 3000,
            "height": 1500,
        },
    )
    assert json.loads(out)["status"] == "ok"
    mock_tools.execute.assert_called_once()
    call_args, call_kwargs = mock_tools.execute.call_args
    assert call_args[0] == "shape_upsert"
    assert call_kwargs["bypass_thread_guard"] is True
    assert call_kwargs["action"] == "create"


def test_execute_impl_calc_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_tools = MagicMock()
    mock_tools.execute.return_value = {"status": "ok"}
    monkeypatch.setattr("plugin.main.get_tools", lambda: mock_tools)
    monkeypatch.setattr(tl.LOBackend, "acquire_document", classmethod(lambda cls, kind=None: object()))
    monkeypatch.setattr(tl.LOBackend, "current_kind", classmethod(lambda cls: "calc"))
    monkeypatch.setattr(tl, "_tool_ctx", lambda doc, kind: object())
    monkeypatch.setattr(tl, "_sheet_headers_and_used", lambda doc: (["Product", "Revenue"], "A1:B5"))

    tl._execute_lo_tool_impl("write_cell_range", {"range": "C1", "values": "Tax"})
    call_args, call_kwargs = mock_tools.execute.call_args
    assert call_args[0] == "write_formula_range"
    assert call_kwargs["bypass_thread_guard"] is True
    assert call_kwargs["range"] == ["C1"]
