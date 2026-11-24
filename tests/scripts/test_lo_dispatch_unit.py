# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LO eval dispatch (mocked get_tools().execute, no soffice)."""
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


def test_execute_impl_uses_execute_unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_tools = MagicMock()
    mock_tools.execute_unguarded.return_value = {"status": "ok", "tree": []}
    monkeypatch.setattr("plugin.app.get_tools", lambda: mock_tools)
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
    mock_tools.execute_unguarded.assert_called_once()
    call_args, call_kwargs = mock_tools.execute_unguarded.call_args
    assert call_args[0] == "shape_upsert"
    assert "bypass_thread_guard" not in call_kwargs
    assert call_kwargs["action"] == "create"


def test_lo_lane_rejects_names_and_args_live_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    import eval_catalog
    from plugin.framework.tool import ToolContext

    monkeypatch.setattr("plugin.app.get_tools", eval_catalog.headless_registry)
    monkeypatch.setattr(tl.LOBackend, "acquire_document", classmethod(lambda cls, kind=None: object()))
    monkeypatch.setattr(tl.LOBackend, "current_kind", classmethod(lambda cls: "calc"))
    monkeypatch.setattr(
        tl,
        "_tool_ctx",
        lambda doc, kind: ToolContext(doc=None, ctx=None, doc_type="calc", services=None, caller="chat"),
    )

    unknown = json.loads(tl._execute_lo_tool_impl("write_cell_range", {"range": ["C1"], "values": "Tax"}))
    assert unknown["code"] == "UNKNOWN_TOOL"

    bad_col = json.loads(
        tl._execute_lo_tool_impl(
            "sort_range",
            {"range": ["A1:B3"], "sort_column": "Revenue", "ascending": False, "has_header": True},
        )
    )
    assert bad_col["code"] == "VALIDATION_ERROR"

    no_rng = json.loads(
        tl._execute_lo_tool_impl(
            "sort_range",
            {"sort_column": 1, "ascending": False, "has_header": True},
        )
    )
    assert no_rng["code"] == "VALIDATION_ERROR"
