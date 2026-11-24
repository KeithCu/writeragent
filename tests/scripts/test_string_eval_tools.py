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

from eval_catalog import headless_registry
from eval_worlds import CalcWorld, WriterWorld
from string_eval_tools import _IMPL, dispatch_string_tool


def test_get_full_and_range():
    s = WriterWorld("<p>Hello</p>")
    r = s.get_content(scope="full")
    assert r["status"] == "ok"
    assert "Hello" in r["content"]
    r2 = s.get_content(scope="range", start=0, end=4)
    assert r2["content"] == "<p>H"


def test_get_document_content_returns_resume_fixture() -> None:
    from dataset import REFORMAT_RESUME

    raw = REFORMAT_RESUME["document_content"]
    s = WriterWorld(raw)
    r = s.get_content(scope="full")
    assert r["status"] == "ok"
    assert r["content"] == raw
    assert "john doe" in r["content"]
    assert "100K users" in r["content"]
    via_dispatch = json.loads(
        dispatch_string_tool(s, "get_content", json.dumps({"scope": "full"}))
    )
    assert via_dispatch["content"] == raw


def test_apply_search_replace():
    s = WriterWorld("foo bar foo")
    r = s.apply_content(
        target="search",
        old_content="foo",
        content="baz",
    )
    assert r["status"] == "ok"
    assert s.get_html() == "baz bar foo"
    r2 = s.apply_content(
        target="search",
        old_content="foo",
        content="x",
        all_matches=True,
    )
    assert r2["status"] == "ok"
    assert "foo" not in s.get_html()


def test_apply_search_all_matches_no_match_errors():
    s = WriterWorld("nothing relevant")
    r = s.apply_content(
        target="search",
        old_content="zzz",
        content="BAR",
        all_matches=True,
    )
    assert r["status"] == "error", r
    assert r["replaced_count"] == 0, r
    assert r["message"].startswith("Replaced 0 occurrence"), r


def test_apply_full_and_end():
    s = WriterWorld("a")
    r = s.apply_content(target="full_document", content="<h1>x</h1>")
    assert r["paragraph_count"] == 1
    assert r["headings"] == [{"level": 1, "text": "x"}]
    assert s.get_html() == "<h1>x</h1>"
    s.apply_content(target="end", content="y")
    assert s.get_html().endswith("y")


def test_find_text():
    s = WriterWorld("AaA")
    r = json.loads(
        dispatch_string_tool(
            s, "search", json.dumps({"pattern": "a", "case_sensitive": False, "max_results": 2})
        )
    )
    assert r["status"] == "ok"
    assert len(r["ranges"]) == 2


def test_dispatch_tools_json():
    s = WriterWorld("hello")
    out = dispatch_string_tool(s, "search", json.dumps({"pattern": "ll"}))
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["ranges"]


def test_calc_write_formula_range_and_snapshot():
    state = CalcWorld("Item\tPrice\nApple\t10\nBanana\t5")
    out = dispatch_string_tool(
        state, "write_range", json.dumps({"range": ["C1"], "values": "Tax"})
    )
    assert json.loads(out)["status"] == "ok"
    dispatch_string_tool(
        state,
        "write_range",
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
    out = dispatch_string_tool(s, "search", json.dumps({"pattern": "there"}))
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["ranges"]


def test_dispatch_apply_style_heading() -> None:
    s = WriterWorld("<p>Background</p><p>Keep me</p>")
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
    s = WriterWorld("hello")
    out = dispatch_string_tool(s, "manual_read", "{}")
    data = json.loads(out)
    assert data["code"] == "unsupported_in_eval"


def test_calc_sort_range_integer_column():
    state = CalcWorld("Product\tRevenue\nWidget\t1200\nTool\t2100")
    res = state.sort_range(range=["A1:B3"], sort_column=1, ascending=False, has_header=True)
    assert res["status"] == "ok"
    assert state._grid[1][0] == "Tool"


def test_calc_sort_mixed_types_descending_text_first() -> None:
    # LibreOffice ScTable::CompareCell: text sorts after numbers, so descending puts it first.
    state = CalcWorld("Product\tRevenue\nWidget\t1200\nAardvark\tn/a\nTool\t2100")
    res = state.sort_range(range=["A1:B4"], sort_column=1, ascending=False, has_header=True)
    assert res["status"] == "ok"
    names = [row[0] for row in state._grid[1:]]
    assert names == ["Aardvark", "Tool", "Widget"]


def test_every_string_tool_is_registered() -> None:
    reg = headless_registry()
    for _world_cls, (_kind, impls) in _IMPL.items():
        for name in impls:
            assert reg.get(name) is not None, f"{name} is not in headless_registry"


def test_dispatch_create_without_geometry_fails_as_live() -> None:
    from eval_worlds import DrawWorld

    base = {"action": "create", "shape_type": "rectangle", "x": 100, "y": 100, "width": 200, "height": 200}
    for missing in ("x", "y", "width", "height"):
        draw = DrawWorld()
        args = {k: v for k, v in base.items() if k != missing}
        res = json.loads(dispatch_string_tool(draw, "shape_upsert", json.dumps(args)))
        assert res["status"] == "error"
        assert res["code"] == "VALIDATION_ERROR"
        assert draw.get_tree()["tree"] == []


def test_dispatch_reports_ignored_parameters() -> None:
    s = WriterWorld("<p>Hello</p>")
    ok = json.loads(
        dispatch_string_tool(s, "get_content", json.dumps({"scope": "full", "bogus_key": 1}))
    )
    assert ok["status"] == "ok"
    assert ok["ignored_parameters"] == ["bogus_key"]

    err = json.loads(
        dispatch_string_tool(s, "search", json.dumps({"bogus_key": 1}))
    )
    assert err["status"] == "error"
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["ignored_parameters"] == ["bogus_key"]


def test_dispatch_coerces_arguments() -> None:
    from eval_worlds import DrawWorld

    draw = DrawWorld()
    json.loads(
        dispatch_string_tool(
            draw,
            "shape_upsert",
            json.dumps({"action": "create", "shape_type": "rectangle", "x": 100, "y": 100, "width": 200, "height": 200}),
        )
    )
    res = json.loads(
        dispatch_string_tool(
            draw,
            "shape_upsert",
            json.dumps({"action": "edit", "index": None, "name": " Shape1 ", "x": "700"}),
        )
    )
    assert res["status"] == "ok"
    assert draw.get_tree()["tree"][0]["geometry"]["x"] == 700

    calc = CalcWorld("A\tB\n1\t20\n2\t10")
    sres = json.loads(
        dispatch_string_tool(
            calc,
            "sort_range",
            json.dumps({"range": ["A1:B3"], "sort_column": 1, "ascending": "false", "has_header": True}),
        )
    )
    assert sres["status"] == "ok"
    assert [r[0] for r in calc._grid[1:]] == ["1", "2"]


def test_dispatch_sort_rejects_header_name_and_missing_range() -> None:
    calc = CalcWorld("Product\tRevenue\nWidget\t1200\nTool\t2100")
    before = [list(r) for r in calc._grid]

    bad_col = json.loads(
        dispatch_string_tool(
            calc,
            "sort_range",
            json.dumps({"range": ["A1:B3"], "sort_column": "Revenue", "ascending": False, "has_header": True}),
        )
    )
    assert bad_col["status"] == "error"
    assert bad_col["code"] == "VALIDATION_ERROR"
    assert calc._grid == before

    no_rng = json.loads(
        dispatch_string_tool(
            calc,
            "sort_range",
            json.dumps({"sort_column": 1, "ascending": False, "has_header": True}),
        )
    )
    assert no_rng["status"] == "error"
    assert no_rng["code"] == "VALIDATION_ERROR"
    assert calc._grid == before


def test_write_range_without_values_or_with_source_leaves_grid() -> None:
    calc = CalcWorld("Item\tPrice\nApple\t10")
    before = [list(r) for r in calc._grid]

    no_val = json.loads(
        dispatch_string_tool(calc, "write_range", json.dumps({"range": ["B2"]}))
    )
    assert no_val["status"] == "error"
    assert calc._grid == before

    with_src = json.loads(
        dispatch_string_tool(calc, "write_range", json.dumps({"range": ["B2"], "source": "A1"}))
    )
    assert with_src["status"] == "error"
    assert with_src["code"] == "unsupported_in_eval"
    assert calc._grid == before


def test_removed_eval_only_names_are_unsupported() -> None:
    w = WriterWorld("hello")
    c = CalcWorld("A\n1")
    r1 = json.loads(dispatch_string_tool(w, "find_text", json.dumps({"search": "he"})))
    r2 = json.loads(dispatch_string_tool(c, "write_cell_range", json.dumps({"range": ["A1"], "values": "x"})))
    assert r1["code"] == "unsupported_in_eval"
    assert r2["code"] == "unsupported_in_eval"

