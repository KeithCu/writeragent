# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for in-memory eval document simulator (scripts/prompt_optimization)."""

import json
import sys
from pathlib import Path

_resolved = Path(__file__).resolve()
if "plugin" in _resolved.parts:
    _REPO = _resolved.parents[3]
else:
    _REPO = _resolved.parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from string_eval_tools import CalcStringState, StringDocState, dispatch_string_tool
from eval_worlds import WriterWorld


def test_get_full_and_range():
    s = StringDocState("<p>Hello</p>")
    r = s.get_document_content(scope="full")
    assert r["status"] == "ok"
    assert "Hello" in r["content"]
    r2 = s.get_document_content(scope="range", start=0, end=4)
    assert r2["content"] == "<p>H"


def test_get_document_content_returns_resume_fixture() -> None:
    from dataset import REFORMAT_RESUME

    raw = REFORMAT_RESUME["document_content"]
    s = WriterWorld(raw)
    r = s.get_document_content(scope="full")
    assert r["status"] == "ok"
    assert r["content"] == raw
    assert "john doe" in r["content"]
    assert "100K users" in r["content"]
    via_dispatch = json.loads(
        dispatch_string_tool(s, "get_document_content", json.dumps({"scope": "full"}))
    )
    assert via_dispatch["content"] == raw


def test_apply_search_replace():
    s = StringDocState("foo bar foo")
    r = s.apply_document_content(
        target="search",
        old_content="foo",
        content="baz",
    )
    assert r["status"] == "ok"
    assert s.get_html() == "baz bar foo"
    r2 = s.apply_document_content(
        target="search",
        old_content="foo",
        content="x",
        all_matches=True,
    )
    assert r2["status"] == "ok"
    assert "foo" not in s.get_html()


def test_apply_search_all_matches_no_match_errors():
    s = StringDocState("nothing relevant")
    r = s.apply_document_content(
        target="search",
        old_content="zzz",
        content="BAR",
        all_matches=True,
    )
    assert r["status"] == "error", r
    assert r["replaced_count"] == 0, r
    assert r["message"].startswith("Replaced 0 occurrence"), r


def test_apply_full_and_end():
    s = StringDocState("a")
    r = s.apply_document_content(target="full_document", content="<h1>x</h1>")
    assert r["paragraph_count"] == 1
    assert r["headings"] == [{"level": 1, "text": "x"}]
    assert s.get_html() == "<h1>x</h1>"
    s.apply_document_content(target="end", content="y")
    assert s.get_html().endswith("y")


def test_find_text():
    s = StringDocState("AaA")
    r = s.find_text("a", case_sensitive=False, limit=2)
    assert r["status"] == "ok"
    assert len(r["ranges"]) == 2


def test_dispatch_tools_json():
    s = StringDocState("hello")
    out = dispatch_string_tool(s, "find_text", json.dumps({"search": "ll"}))
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["ranges"]


def test_calc_write_formula_range_and_snapshot():
    state = CalcStringState("Item\tPrice\nApple\t10\nBanana\t5")
    out = dispatch_string_tool(
        state, "write_formula_range", json.dumps({"range": ["C1"], "values": "Tax"})
    )
    assert json.loads(out)["status"] == "ok"
    dispatch_string_tool(
        state,
        "write_cell_range",
        json.dumps({"range": ["C2:C3"], "values": "[0.8, 0.4]"}),
    )
    snap = state.snapshot()
    assert snap["snapshot"] is True
    assert "Tax" in snap["headers"]
    assert 0.8 in snap["rows"][1]
    dump = json.dumps(snap)
    assert "snapshot" in dump
    assert "Tax" in dump


def test_dispatch_search_in_document_alias():
    s = WriterWorld("hello there")
    out = dispatch_string_tool(s, "search_in_document", json.dumps({"pattern": "there"}))
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["ranges"]


def test_dispatch_apply_style_heading() -> None:
    s = StringDocState("<p>Background</p><p>Keep me</p>")
    out = dispatch_string_tool(
        s,
        "apply_style",
        json.dumps({"style": "Heading 1", "target": "search", "old_content": "Background"}),
    )
    data = json.loads(out)
    assert data["status"] == "ok"
    html = s.get_html()
    assert "<h1>" in html
    assert "Background" in html


def test_dispatch_unsupported_writer_tool():
    s = StringDocState("hello")
    out = dispatch_string_tool(s, "get_guidance", "{}")
    data = json.loads(out)
    assert data["code"] == "unsupported_in_eval"


def test_calc_sort_range_integer_column():
    state = CalcStringState("Product\tRevenue\nWidget\t1200\nTool\t2100")
    res = state.range_sort(sort_column=1, ascending=False)
    assert res["status"] == "ok"
    assert state._grid[1][0] == "Tool"


def test_calc_sort_mixed_types_non_numeric_last() -> None:
    state = CalcStringState("Product\tRevenue\nWidget\t1200\nAardvark\tn/a\nTool\t2100")
    res = state.range_sort(sort_column=1, ascending=False)
    assert res["status"] == "ok"
    names = [row[0] for row in state._grid[1:]]
    assert names[-1] == "Aardvark"
    assert names[0] == "Tool"

