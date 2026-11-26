# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for in-memory eval document simulator (scripts/prompt_optimization)."""

import json
import sys
from pathlib import Path

import pytest

_resolved = Path(__file__).resolve()
if "plugin" in _resolved.parts:
    _REPO = _resolved.parents[3]
else:
    _REPO = _resolved.parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_catalog import headless_registry
from eval_worlds import CalcWorld, DrawWorld, WriterWorld
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


def test_search_case_insensitive_and_max_results():
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


def test_dispatch_search():
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
    assert data["applied_count"] == 1
    html = s.get_html()
    assert "<h1>Background</h1>" in html
    assert "<p>Keep me</p>" in html


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
    assert no_val["code"] == "INVALID_PARAM"
    assert no_val["message"] == "values is required when source is omitted"
    assert calc._grid == before

    null_val = json.loads(
        dispatch_string_tool(calc, "write_range", json.dumps({"range": ["B2"], "values": None}))
    )
    assert null_val["status"] == "error"
    assert null_val["code"] == "INVALID_PARAM"
    assert null_val["message"] == "values is required when source is omitted"
    assert calc._grid == before

    empty_rng = json.loads(
        dispatch_string_tool(calc, "write_range", json.dumps({"range": [], "values": "x"}))
    )
    assert empty_rng["status"] == "error"
    assert empty_rng["code"] == "INVALID_PARAM"
    assert empty_rng["message"] == "range is required"
    assert calc._grid == before

    with_src = json.loads(
        dispatch_string_tool(calc, "write_range", json.dumps({"range": ["B2"], "source": "A1"}))
    )
    assert with_src["status"] == "error"
    assert with_src["code"] == "unsupported_in_eval"
    assert calc._grid == before


def test_get_content_range_requires_valid_bounds() -> None:
    s = WriterWorld("<p>Hello</p>")
    missing = json.loads(
        dispatch_string_tool(s, "get_content", json.dumps({"scope": "range", "start": 0}))
    )
    assert missing["status"] == "error"
    assert "integer start and end" in missing["message"]

    bad_order = json.loads(
        dispatch_string_tool(s, "get_content", json.dumps({"scope": "range", "start": 3, "end": 3}))
    )
    assert bad_order["status"] == "error"
    assert "0 <= start < end" in bad_order["message"]


def test_removed_eval_only_names_are_unsupported() -> None:
    w = WriterWorld("hello")
    c = CalcWorld("A\n1")
    r1 = json.loads(dispatch_string_tool(w, "find_text", json.dumps({"search": "he"})))
    r2 = json.loads(dispatch_string_tool(c, "write_cell_range", json.dumps({"range": ["A1"], "values": "x"})))
    assert r1["code"] == "unsupported_in_eval"
    assert r2["code"] == "unsupported_in_eval"


@pytest.mark.parametrize(
    "args",
    [
        {"target": "search", "start_anchor": "foo", "end_anchor": "bar", "content": "X"},
        {"target": "search", "old_content": "foo", "content": "X", "dry_run": True},
        {"target": "search", "old_content": "foo", "content": "X", "position": "before"},
        {"target": "search", "old_content": "foo", "content": "X", "position": "after"},
        {"target": "search", "old_content": "foo", "content": "X", "regex": True},
        {"target": "search", "old_content": "foo", "content": "X", "hyperlink_url": "https://example.com"},
    ],
)
def test_apply_content_unsupported_arguments_leave_document(args: dict[str, object]) -> None:
    w = WriterWorld("<p>foo bar</p>")
    res = json.loads(dispatch_string_tool(w, "apply_content", json.dumps(args)))
    assert res["status"] == "error"
    assert res["code"] == "unsupported_in_eval"
    assert w.get_html() == "<p>foo bar</p>"


def test_apply_content_accepts_default_valued_arguments() -> None:
    w = WriterWorld("<p>foo bar</p>")
    res = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps(
                {
                    "target": "search",
                    "old_content": "foo",
                    "content": "baz",
                    "position": "replace",
                    "regex": False,
                    "dry_run": False,
                    "occurrence": 0,
                }
            ),
        )
    )
    assert res["status"] == "ok"
    assert w.get_html() == "<p>baz bar</p>"


def test_apply_content_occurrence_picks_match() -> None:
    w = WriterWorld("foo ... FOO ... foo")
    res = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "foo", "content": "BAR", "occurrence": 1}),
        )
    )
    assert res["status"] == "ok"
    assert res["occurrence"] == 1
    assert w.get_html() == "foo ... FOO ... BAR"

    too_high = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "foo", "content": "X", "occurrence": 5}),
        )
    )
    assert too_high["status"] == "error"
    assert too_high["code"] == "OCCURRENCE_OUT_OF_RANGE"
    assert w.get_html() == "foo ... FOO ... BAR"

    neg = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "foo", "content": "X", "occurrence": -1}),
        )
    )
    assert neg["status"] == "error"
    assert neg["code"] == "INVALID_PARAM"

    with_all = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps(
                {"target": "search", "old_content": "foo", "content": "X", "all_matches": True, "occurrence": 0}
            ),
        )
    )
    assert with_all["status"] == "error"
    assert with_all["code"] == "INVALID_PARAM"

    on_end = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "end", "content": "X", "occurrence": 0}),
        )
    )
    assert on_end["status"] == "error"
    assert on_end["code"] == "INVALID_PARAM"
    assert w.get_html() == "foo ... FOO ... BAR"


def test_apply_content_case_sensitive() -> None:
    w = WriterWorld("<p>Hello</p>")
    wrong_case = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "hello", "content": "Hi", "case_sensitive": True}),
        )
    )
    assert wrong_case["status"] == "error"
    assert wrong_case["code"] == "OLD_CONTENT_NOT_FOUND"
    assert w.get_html() == "<p>Hello</p>"

    ci = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "hello", "content": "Hi", "case_sensitive": False}),
        )
    )
    assert ci["status"] == "ok"
    assert w.get_html() == "<p>Hi</p>"

    w2 = WriterWorld("<p>Hello</p>")
    omitted = json.loads(
        dispatch_string_tool(
            w2,
            "apply_content",
            json.dumps({"target": "search", "old_content": "hello", "content": "Hi"}),
        )
    )
    assert omitted["status"] == "ok"
    assert w2.get_html() == "<p>Hi</p>"


def test_search_rejects_regex_and_bad_max_results() -> None:
    w = WriterWorld("<p>Hello world</p>")
    bad_limit = json.loads(
        dispatch_string_tool(w, "search", json.dumps({"pattern": "Hello", "max_results": 0}))
    )
    assert bad_limit["status"] == "error"
    assert bad_limit["code"] == "INVALID_PARAM"

    bad_regex = json.loads(
        dispatch_string_tool(w, "search", json.dumps({"pattern": "H.*o", "regex": True}))
    )
    assert bad_regex["status"] == "error"
    assert bad_regex["code"] == "unsupported_in_eval"


def test_get_content_rejects_bad_max_chars() -> None:
    w = WriterWorld("<p>Hello</p>")
    for bad in (0, -3):
        res = json.loads(
            dispatch_string_tool(w, "get_content", json.dumps({"scope": "full", "max_chars": bad}))
        )
        assert res["status"] == "error"
        assert res["code"] == "TOOL_EXECUTION_ERROR"


def test_comment_add_errors_carry_codes() -> None:
    w = WriterWorld("<p>alpha beta</p>")
    no_search = json.loads(dispatch_string_tool(w, "comment_add", json.dumps({"content": "note"})))
    assert no_search["status"] == "error"
    assert no_search["code"] == "TOOL_EXECUTION_ERROR"

    not_found = json.loads(
        dispatch_string_tool(w, "comment_add", json.dumps({"search": "gamma", "content": "note"}))
    )
    assert not_found["status"] == "error"
    assert not_found["code"] == "OLD_CONTENT_NOT_FOUND"

    too_high = json.loads(
        dispatch_string_tool(
            w, "comment_add", json.dumps({"search": "alpha", "content": "note", "occurrence": 2})
        )
    )
    assert too_high["status"] == "error"
    assert too_high["code"] == "OCCURRENCE_OUT_OF_RANGE"

    with_parent = json.loads(
        dispatch_string_tool(
            w,
            "comment_add",
            json.dumps({"search": "alpha", "content": "note", "parent_name": "c1"}),
        )
    )
    assert with_parent["status"] == "error"
    assert with_parent["code"] == "unsupported_in_eval"
    assert w.get_html() == "<p>alpha beta</p>"


def test_write_range_array_true() -> None:
    c = CalcWorld("A\tB\n1\t2")
    before = [list(r) for r in c._grid]

    bad_formula = json.loads(
        dispatch_string_tool(c, "write_range", json.dumps({"range": ["C2"], "values": "x", "array": True}))
    )
    assert bad_formula["status"] == "error"
    assert bad_formula["code"] == "CALC_ERROR"
    assert c._grid == before

    formula = json.loads(
        dispatch_string_tool(c, "write_range", json.dumps({"range": ["C2"], "values": "=B2*2", "array": True}))
    )
    assert formula["status"] == "error"
    assert formula["code"] == "unsupported_in_eval"
    assert c._grid == before

    ok = json.loads(
        dispatch_string_tool(c, "write_range", json.dumps({"range": ["C2"], "values": "=B2*2", "array": False}))
    )
    assert ok["status"] == "ok"
    assert c.formulas["C2"] == "=B2*2"


def test_write_range_single_item_list_does_not_fill() -> None:
    c = CalcWorld("A\tB\n1\t2\n3\t4")
    before = [list(r) for r in c._grid]
    res = json.loads(
        dispatch_string_tool(c, "write_range", json.dumps({"range": ["C1:C2"], "values": ["x"]}))
    )
    assert res["status"] == "error"
    assert res["code"] == "CALC_ERROR"
    assert c._grid == before


def test_apply_style_errors_carry_codes() -> None:
    w = WriterWorld("<p>Hello</p>")
    empty_style = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "  ", "target": "search", "old_content": "Hello"}))
    )
    assert empty_style["status"] == "error"
    assert empty_style["code"] == "INVALID_PARAM"

    no_old = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "Heading 1", "target": "search"}))
    )
    assert no_old["status"] == "error"
    assert no_old["code"] == "INVALID_PARAM"

    sel = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "Heading 1", "target": "selection"}))
    )
    assert sel["status"] == "error"
    assert sel["code"] == "unsupported_in_eval"

    full = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "Heading 1", "target": "full_document"}))
    )
    assert full["status"] == "error"
    assert full["code"] == "unsupported_in_eval"
    assert w.get_html() == "<p>Hello</p>"


def test_apply_content_invalid_param_and_all_matches_rebuild() -> None:
    w = WriterWorld("a ... a")
    no_target = json.loads(dispatch_string_tool(w, "apply_content", json.dumps({"content": "x"})))
    assert no_target["status"] == "error"
    assert no_target["code"] == "INVALID_PARAM"

    no_old = json.loads(
        dispatch_string_tool(w, "apply_content", json.dumps({"target": "search", "content": "x"}))
    )
    assert no_old["status"] == "error"
    assert no_old["code"] == "INVALID_PARAM"

    blank_old = json.loads(
        dispatch_string_tool(
            w, "apply_content", json.dumps({"target": "search", "old_content": "   ", "content": "x"})
        )
    )
    assert blank_old["status"] == "error"
    assert blank_old["code"] == "INVALID_PARAM"

    miss_all = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "zzz", "content": "x", "all_matches": True}),
        )
    )
    assert miss_all["status"] == "error"
    assert miss_all["code"] == "OLD_CONTENT_NOT_FOUND"
    assert miss_all["replaced_count"] == 0

    rebuilt = json.loads(
        dispatch_string_tool(
            w,
            "apply_content",
            json.dumps({"target": "search", "old_content": "a", "content": "XYZ", "all_matches": True}),
        )
    )
    assert rebuilt["status"] == "ok"
    assert rebuilt["replaced_count"] == 2
    assert w.get_html() == "XYZ ... XYZ"


def test_comment_add_occurrence_case_fallback_and_negative() -> None:
    w = WriterWorld("<p>Alpha ... Beta ... Alpha</p>")
    neg = json.loads(
        dispatch_string_tool(
            w, "comment_add", json.dumps({"search": "Alpha", "content": "n", "occurrence": -1})
        )
    )
    assert neg["status"] == "error"
    assert neg["code"] == "INVALID_PARAM"
    assert w.get_html() == "<p>Alpha ... Beta ... Alpha</p>"

    second = json.loads(
        dispatch_string_tool(
            w, "comment_add", json.dumps({"search": "Alpha", "content": "second", "occurrence": 1})
        )
    )
    assert second["status"] == "ok"
    assert second["anchor_text"] == "Alpha"
    assert w.get_html() == "<p>Alpha ... Beta ... Alpha [second]</p>"

    ci = json.loads(
        dispatch_string_tool(w, "comment_add", json.dumps({"search": "beta", "content": "ci"}))
    )
    assert ci["status"] == "ok"
    assert ci["anchor_text"] == "Beta"
    assert w.get_html() == "<p>Alpha ... Beta [ci] ... Alpha [second]</p>"


def test_search_non_overlapping_hits() -> None:
    w = WriterWorld("aaaa")
    res = json.loads(dispatch_string_tool(w, "search", json.dumps({"pattern": "aa"})))
    assert res["status"] == "ok"
    assert [(r["start"], r["end"]) for r in res["ranges"]] == [(0, 2), (2, 4)]


def test_write_range_array_true_leading_space_is_calc_error() -> None:
    c = CalcWorld("A\tB\n1\t2")
    before = [list(r) for r in c._grid]
    res = json.loads(
        dispatch_string_tool(c, "write_range", json.dumps({"range": ["C2"], "values": " =B2*2", "array": True}))
    )
    assert res["status"] == "error"
    assert res["code"] == "CALC_ERROR"
    assert c._grid == before


def test_apply_style_miss_leaves_document() -> None:
    w = WriterWorld("<p>Alpha</p>")
    miss = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "Heading 1", "target": "search", "old_content": "Nope"}))
    )
    assert miss["status"] == "error"
    assert miss["code"] == "OLD_CONTENT_NOT_FOUND"
    assert miss["applied"] is False
    assert miss["matched"] is False
    assert w.get_html() == "<p>Alpha</p>"

    wrong_case = json.loads(
        dispatch_string_tool(
            w,
            "apply_style",
            json.dumps({"style": "Heading 1", "target": "search", "old_content": "alpha", "case_sensitive": True}),
        )
    )
    assert wrong_case["status"] == "error"
    assert wrong_case["code"] == "OLD_CONTENT_NOT_FOUND"
    assert wrong_case["applied"] is False
    assert wrong_case["matched"] is False
    assert w.get_html() == "<p>Alpha</p>"

    fallback = json.loads(
        dispatch_string_tool(w, "apply_style", json.dumps({"style": "Heading 1", "target": "search", "old_content": "alpha"}))
    )
    assert fallback["status"] == "ok"
    assert w.get_html() == "<h1>Alpha</h1>"


def test_apply_style_occurrence_counts_matches() -> None:
    doc = "<p>foo foo</p><p>foo</p>"
    w = WriterWorld(doc)
    second = json.loads(
        dispatch_string_tool(
            w, "apply_style", json.dumps({"style": "Heading 2", "target": "search", "old_content": "foo", "occurrence": 2})
        )
    )
    assert second["status"] == "ok"
    assert w.get_html() == "<p>foo foo</p><h2>foo</h2>"

    for bad_occ in (3, -1):
        w_err = WriterWorld(doc)
        err = json.loads(
            dispatch_string_tool(
                w_err,
                "apply_style",
                json.dumps({"style": "Heading 2", "target": "search", "old_content": "foo", "occurrence": bad_occ}),
            )
        )
        assert err["status"] == "error"
        assert err["code"] == "OCCURRENCE_OUT_OF_RANGE"
        assert w_err.get_html() == doc

    w_both = WriterWorld(doc)
    occ_wins = json.loads(
        dispatch_string_tool(
            w_both,
            "apply_style",
            json.dumps({"style": "Heading 2", "target": "search", "old_content": "foo", "all_matches": True, "occurrence": 2}),
        )
    )
    assert occ_wins["status"] == "ok"
    assert w_both.get_html() == "<p>foo foo</p><h2>foo</h2>"

    w_all = WriterWorld(doc)
    all_res = json.loads(
        dispatch_string_tool(
            w_all,
            "apply_style",
            json.dumps({"style": "Heading 2", "target": "search", "old_content": "foo", "all_matches": True}),
        )
    )
    assert all_res["status"] == "ok"
    assert all_res["applied_count"] == 3
    assert w_all.get_html() == "<h2>foo foo</h2><h2>foo</h2>"


def test_apply_style_matches_visible_text() -> None:
    w_attr = WriterWorld('<p data-lo-style="Default">keep</p><p>default y</p>')
    res_attr = json.loads(
        dispatch_string_tool(
            w_attr, "apply_style", json.dumps({"style": "Heading 1", "target": "search", "old_content": "Default"})
        )
    )
    assert res_attr["status"] == "ok"
    assert w_attr.get_html() == '<p data-lo-style="Default">keep</p><h1>default y</h1>'

    w_inline = WriterWorld("<p>a <b>Alpha</b> two</p>")
    res_inline = json.loads(
        dispatch_string_tool(
            w_inline, "apply_style", json.dumps({"style": "Heading 1", "target": "search", "old_content": "a Alpha two"})
        )
    )
    assert res_inline["status"] == "ok"
    assert w_inline.get_html() == "<h1>a <b>Alpha</b> two</h1>"

    w_html_needle = WriterWorld("<h2>Heading 2 again.</h2>")
    res_needle = json.loads(
        dispatch_string_tool(
            w_html_needle,
            "apply_style",
            json.dumps({"style": "Heading 1", "target": "search", "old_content": "<h2>Heading 2 again.</h2>"}),
        )
    )
    assert res_needle["status"] == "ok"
    assert w_html_needle.get_html() == "<h1>Heading 2 again.</h1>"

    w_bare = WriterWorld("<p>Top</p>\n\nBare line\n\n<p>Bottom</p>")
    res_bare = json.loads(
        dispatch_string_tool(
            w_bare, "apply_style", json.dumps({"style": "Heading 1", "target": "search", "old_content": "Bare line"})
        )
    )
    assert res_bare["status"] == "ok"
    assert w_bare.get_html() == "<p>Top</p>\n\n<h1>Bare line</h1>\n\n<p>Bottom</p>"


@pytest.mark.parametrize(
    ("args", "expected_code"),
    [
        ({"style": "Emphasis", "family": "CharacterStyles", "target": "search", "old_content": "Alpha"}, "unsupported_in_eval"),
        ({"style": "Heading 1", "target": "search", "old_content": "Alpha", "clear_direct": "all"}, "unsupported_in_eval"),
        ({"style": "Heading 1", "old_content": "Alpha"}, "unsupported_in_eval"),
        ({"style": "Heading 1", "target": "search", "old_content": "Alpha\nBeta"}, "unsupported_in_eval"),
        ({"style": "Quotations", "target": "full_document", "clear_direct": "all"}, "INVALID_PARAM"),
    ],
)
def test_apply_style_unsupported_arguments(args: dict[str, object], expected_code: str) -> None:
    doc = '<p data-lo-style="Default">Alpha</p><p>Beta</p>'
    w = WriterWorld(doc)
    res = json.loads(dispatch_string_tool(w, "apply_style", json.dumps(args)))
    assert res["status"] == "error"
    assert res["code"] == expected_code
    assert w.get_html() == doc


def test_apply_style_clear_direct_none_is_ok() -> None:
    w = WriterWorld('<p data-lo-style="Default">Alpha</p><p>Beta</p>')
    ok = json.loads(
        dispatch_string_tool(
            w,
            "apply_style",
            json.dumps({"style": "Heading 1", "target": "search", "old_content": "Alpha", "clear_direct": "none"}),
        )
    )
    assert ok["status"] == "ok"
    assert w.get_html() == "<h1>Alpha</h1><p>Beta</p>"


@pytest.mark.parametrize("target", ["beginning", "end", "selection"])
@pytest.mark.parametrize("content", ["", [], "   ", [""]])
def test_apply_content_empty_insert_is_refused(target: str, content: object) -> None:
    w = WriterWorld("<p>Keep</p>")
    res = json.loads(
        dispatch_string_tool(w, "apply_content", json.dumps({"target": target, "content": content}))
    )
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert w.get_html() == "<p>Keep</p>"


def test_apply_content_empty_full_document_clears() -> None:
    w = WriterWorld("<p>Keep</p>")
    ok = json.loads(
        dispatch_string_tool(w, "apply_content", json.dumps({"target": "full_document", "content": ""}))
    )
    assert ok["status"] == "ok"
    assert w.get_html() == ""


@pytest.mark.parametrize(
    ("rng", "values"),
    [
        (["A1"], [[["x"]]]),
        (["A1"], [{"a": 1}]),
        (["A1"], '[{"a":1}]'),
        (["A1:A2"], '[{"x":1},2]'),
    ],
)
def test_write_range_rejects_nested_cells(rng: list[str], values: object) -> None:
    c = CalcWorld("A\tB\n1\t2")
    before = [list(r) for r in c._grid]
    res = json.loads(dispatch_string_tool(c, "write_range", json.dumps({"range": rng, "values": values})))
    assert res["status"] == "error"
    assert res["code"] == "CALC_ERROR"
    assert c._grid == before
    assert c.writes == []


def test_write_range_allows_null_cell() -> None:
    c = CalcWorld("A\tB\n1\t2")
    ok = json.loads(dispatch_string_tool(c, "write_range", json.dumps({"range": ["A1:B1"], "values": [["a", None]]})))
    assert ok["status"] == "ok"
    assert c._grid[0] == ["a", None]
