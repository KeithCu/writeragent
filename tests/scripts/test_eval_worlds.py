# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Worlds for string eval: HTML blocks, Draw edges, Calc dest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_worlds import CalcWorld, DrawWorld, WriterWorld, parse_a1_range
from plugin.calc.manipulator import _sort_unchanged_message


def test_html_apply_round_trip_heading_and_table() -> None:
    world = WriterWorld("plain")
    html = "<h1>Intro</h1><table><tr><td>A</td></tr></table>"
    world.apply_content(target="full_document", content=html)
    types = [b["type"] for b in world.blocks]
    assert "heading" in types
    assert "table" in types
    assert "<h1>Intro</h1>" in world.export_for_prompt()


def test_style_field_from_class() -> None:
    world = WriterWorld("")
    world.apply_content(
        target="full_document",
        content='<p class="Quotations">Default style paragraph one.</p>',
    )
    styled = [b for b in world.blocks if b["style"] == "Quotations"]
    assert styled
    assert "Quotations" in world.get_html()


def test_add_comment_on_uncertain() -> None:
    world = WriterWorld("The results are uncertain at this point.")
    res = world.comment_add(search="uncertain", content="Review this before finalizing")
    assert res["comment_added"] is True
    assert "[Review this before finalizing]" in world.get_html()
    commented = [b for b in world.blocks if b["comments"]]
    assert commented


def test_search_in_document_offsets() -> None:
    world = WriterWorld("<p>Hello world</p>")
    found = world.search(pattern="world")
    assert found["status"] == "ok"
    assert found["ranges"]


def _draw_boxes(*texts: str) -> DrawWorld:
    draw = DrawWorld()
    for i, text in enumerate(texts):
        res = draw.shape_upsert(
            action="create", shape_type="rectangle", text=text, x=1000 + 3000 * i, y=1000, width=2000, height=1000
        )
        assert res["index"] == i
    return draw


def test_draw_connect_tree() -> None:
    draw = DrawWorld()
    draw.shape_upsert(action="create", shape_type="ellipse", text="Start")
    draw.shape_upsert(action="create", shape_type="flowchart-process", text="Process")
    res = draw.shape_connect(start="Shape1", end="1")
    assert res["status"] == "ok"
    assert res["index"] == 2
    assert res["name"] == "Shape3"
    assert res["page"] == 0
    nodes = draw.get_tree()["tree"]
    assert len(nodes) == 3
    assert nodes[2]["type"] == "ConnectorShape"
    assert nodes[2]["connected_start"] == {"name": "Shape1", "text": "Start"}
    assert nodes[2]["connected_end"] == {"name": "Shape2", "text": "Process"}

    nxt = draw.shape_upsert(action="create", shape_type="rectangle", text="After")
    assert nxt["index"] == 3


def test_draw_connect_records_arrowheads() -> None:
    draw = _draw_boxes("Start", "Process")
    res_end = draw.shape_connect(start=0, end=1, line_end="arrow", line_start=None)
    assert res_end["status"] == "ok"
    assert draw.get_tree()["tree"][2]["style"] == {"LineEndName": "Arrow"}

    res_start = draw.shape_connect(start=1, end=0, line_start="arrow", line_end="none")
    assert res_start["status"] == "ok"
    assert draw.get_tree()["tree"][3]["style"] == {"LineStartName": "Arrow"}

    res_bad = draw.shape_connect(start=0, end=1, line_end="triangle")
    assert res_bad["status"] == "error"
    assert len(draw.get_tree()["tree"]) == 4


@pytest.mark.parametrize("bad_end", [99, "9", "0.0", "Nope", "", True])
def test_draw_connect_unknown_shape_changes_nothing(bad_end: object) -> None:
    draw = _draw_boxes("Start", "Process")
    res = draw.shape_connect(start=0, end=bad_end)
    assert res["status"] == "error"
    assert len(draw.get_tree()["tree"]) == 2


def test_draw_connect_unknown_name_lists_page_names() -> None:
    draw = _draw_boxes("Start", "Process")
    res = draw.shape_connect(start=0, end="Shap1")
    assert res["status"] == "error"
    assert res["details"]["available"] == ["Shape1", "Shape2"]


def test_draw_edit_updates_shape() -> None:
    draw = DrawWorld()
    draw.shape_upsert(action="create", shape_type="rectangle", text="Old", x=100, y=200, width=300, height=400)
    draw.shape_upsert(action="create", shape_type="rectangle", text="Next")
    draw.shape_connect(start=0, end=1)

    res = draw.shape_upsert(action="edit", index=0, text="New", x=500, width=900)
    assert res["status"] == "ok"
    tree = draw.get_tree()["tree"]
    assert tree[0]["text"] == "New"
    assert tree[0]["geometry"] == {"x": 500, "y": 200, "width": 900, "height": 400}
    assert tree[2]["connected_start"]["text"] == "New"

    res_clear = draw.shape_upsert(action="edit", index=0, text="")
    assert res_clear["status"] == "ok"
    cleared = draw.get_tree()["tree"][0]
    assert "text" not in cleared
    assert cleared["fillable"] is True


@pytest.mark.parametrize(
    "selector",
    [
        {"index": "1"},
        {"index": 1.0},
        {"name": "Shape2"},
        {"index": None, "name": " Shape2 "},
    ],
)
def test_draw_edit_by_name_or_string_index(selector: dict[str, object]) -> None:
    draw = _draw_boxes("One", "Two")
    res = draw.shape_upsert(action="edit", x="700", **selector)
    assert res["status"] == "ok"
    assert res["index"] == 1
    assert res["name"] == "Shape2"
    assert draw.get_tree()["tree"][1]["geometry"]["x"] == 700


def test_draw_names_are_unique() -> None:
    draw = _draw_boxes("One", "Two")
    dup_create = draw.shape_upsert(action="create", shape_type="rectangle", name="Shape1")
    assert dup_create["status"] == "error"
    assert dup_create["code"] == "INVALID_PARAM"

    dup_rename = draw.shape_upsert(action="edit", index=1, name="Shape1")
    assert dup_rename["status"] == "error"
    assert dup_rename["code"] == "INVALID_PARAM"

    renamed = draw.shape_upsert(action="edit", index=0, name="StartBox")
    assert renamed["status"] == "ok"
    assert renamed["name"] == "StartBox"

    nxt = draw.shape_upsert(action="create", shape_type="rectangle")
    assert nxt["name"] == "Shape3"


def test_draw_negative_span_reads_back_normalised() -> None:
    draw = DrawWorld()
    c_rect = draw.shape_upsert(action="create", shape_type="rectangle", x=6000, y=4000, width=-2000, height=-1000)
    assert c_rect["status"] == "ok"
    assert draw.get_tree()["tree"][0]["geometry"] == {"x": 4000, "y": 3000, "width": 2000, "height": 1000}

    c_line = draw.shape_upsert(action="create", shape_type="line", x=5000, y=9000, width=-2000, height=-1000)
    assert c_line["status"] == "ok"
    assert draw.get_tree()["tree"][1]["geometry"] == {"x": 3000, "y": 8000, "width": 2000, "height": 1000}

    e_line = draw.shape_upsert(action="edit", index=1, width=-3000)
    assert e_line["status"] == "ok"
    assert draw.get_tree()["tree"][1]["geometry"] == {"x": 0, "y": 8000, "width": 3000, "height": 1000}


def test_draw_group_is_a_tree_node() -> None:
    draw = _draw_boxes("A", "B", "C", "D")
    res = draw.shape_group(indices=[2, "Shape1"])
    assert res["status"] == "ok"
    assert res["index"] == 1
    assert res["name"] == "Shape5"

    tree = draw.get_tree()["tree"]
    assert [n["name"] for n in tree] == ["Shape2", "Shape5", "Shape4"]
    group = tree[1]
    assert group["type"] == "GroupShape"
    assert [(c["path_index"], c["name"]) for c in group["children"]] == [("1.0", "Shape1"), ("1.1", "Shape3")]

    moved = draw.shape_upsert(action="edit", index=1, x=2000)
    assert moved["status"] == "ok"
    tree2 = draw.get_tree()["tree"]
    assert [c["geometry"]["x"] for c in tree2[1]["children"]] == [2000, 8000]

    edited_child = draw.shape_upsert(action="edit", index="1.1", text="C2")
    assert edited_child["status"] == "ok"
    assert draw.get_tree()["tree"][1]["children"][1]["text"] == "C2"

    summary_idxs = [row["index"] for row in draw.get_draw_summary()["shapes"]]
    assert summary_idxs == [0, 1, "1.0", "1.1", 2]


def test_draw_group_rejects_bad_members() -> None:
    draw = _draw_boxes("A", "B")
    assert draw.shape_group(indices=[0, 1])["status"] == "ok"
    draw.shape_upsert(action="create", shape_type="rectangle", text="C")
    before = draw.get_tree()
    for bad in (None, [0], [0, 0], [0, 9], ["1.0", 0]):
        res = draw.shape_group(indices=bad)
        assert res["status"] == "error"
        assert draw.get_tree() == before


def test_draw_upsert_records_arrowheads() -> None:
    draw = DrawWorld()
    res = draw.shape_upsert(action="create", shape_type="line", line_end="arrow", line_start="none")
    assert res["status"] == "ok"
    assert draw.get_tree()["tree"][0]["style"] == {"LineEndName": "Arrow"}

    res_edit = draw.shape_upsert(action="edit", index=0, line_end="none", line_start="arrow")
    assert res_edit["status"] == "ok"
    assert draw.get_tree()["tree"][0]["style"] == {"LineStartName": "Arrow"}

    bad_create = draw.shape_upsert(action="create", shape_type="line", line_end="triangle")
    assert bad_create["status"] == "error"
    assert len(draw.get_tree()["tree"]) == 1

    bad_edit = draw.shape_upsert(action="edit", index=0, line_end="triangle")
    assert bad_edit["status"] == "error"
    assert draw.get_tree()["tree"][0]["style"] == {"LineStartName": "Arrow"}


@pytest.mark.parametrize(
    ("shape_type", "expected_type", "expected_preset"),
    [
        ("rectangle", "CustomShape", "rectangle"),
        ("ellipse", "CustomShape", "ellipse"),
        ("diamond", "CustomShape", "diamond"),
        ("flowchart-connector", "CustomShape", "flowchart-connector"),
        ("text", "TextShape", None),
        ("line", "LineShape", None),
        ("connector", "ConnectorShape", None),
        ("EllipseShape", "EllipseShape", None),
        ("com.sun.star.drawing.RectangleShape", "RectangleShape", None),
    ],
)
def test_draw_node_types_match_production(
    shape_type: str,
    expected_type: str,
    expected_preset: str | None,
) -> None:
    import oracles

    draw = DrawWorld()
    draw.shape_upsert(action="create", shape_type=shape_type, text="Node")
    node = draw.get_tree()["tree"][0]
    assert node["type"] == expected_type
    assert node.get("custom_shape_type") == expected_preset

    summary = draw.get_draw_summary()["shapes"][0]
    assert summary["type"] == f"com.sun.star.drawing.{expected_type}"
    assert summary.get("custom_shape_type") == expected_preset

    if shape_type == "flowchart-connector":
        assert oracles.is_draw_connector(node) is False


def test_calc_py_dest_snapshot() -> None:
    calc = CalcWorld("Name\tAmt\nAnn\t1")
    calc.write_range(
        range=["J1"],
        values='=PY("result = 1"; A1:H500)',
    )
    snap = calc.snapshot()
    assert snap["formulas"]["J1"].startswith("=PY")
    assert snap["writes"][0]["dests"] == ["J1"]
    dump = json.dumps(snap)
    assert "J1" in dump
    assert "=PY" in dump


def test_apply_style_heading_html() -> None:
    world = WriterWorld("<p>Background</p><p>Other</p>")
    res = world.apply_style(style="Heading 1", target="search", old_content="Background")
    assert res["status"] == "ok"
    html = world.get_html()
    assert "<h1>" in html
    assert "Background" in html
    assert "Other" in html


def test_sort_two_pass_stable_device_before_widget() -> None:
    calc = CalcWorld(
        "Product\tRevenue\nWidget\t1200\nGadget\t850\nTool\t2100\nDevice\t1200\nAardvark\tn/a"
    )
    calc.sort_range(sort_column=0, ascending=True, has_header=True)
    calc.sort_range(sort_column=1, ascending=False, has_header=True)
    names = [row[0] for row in calc._grid[1:]]
    assert names == ["Aardvark", "Tool", "Device", "Widget", "Gadget"]


def test_calc_write_formula_range_rejects_length_mismatch() -> None:
    calc = CalcWorld("Item\tAmt\nAnn\t1\nBea\t2\nCal\t3\nDee\t4")
    res = calc.write_range(range=["C2:C9"], values=["a", "b", "c", "d"])
    assert res["status"] == "error"
    assert "4 values" in res["message"]
    assert "8 cells" in res["message"]
    # Must not zip-truncate into the extra rows.
    assert calc.writes == []


def test_calc_write_formula_range_accepts_scalar_fill() -> None:
    calc = CalcWorld("Item\tAmt\nAnn\t1")
    res = calc.write_range(range=["C2:C5"], values="=B2+1")
    assert res["status"] == "ok"
    assert res["written"] == 4


def test_calc_single_formula_fill_down_adjusts_relative_refs() -> None:
    """One formula string into a 1-D Tax column matches production fill-down."""
    calc = CalcWorld("Item\tPrice\nApple\t10\nBanana\t5\nOrange\t8\nPear\t12.5")
    res = calc.write_range(range=["C2:C5"], values="=B2*0.08")
    assert res["status"] == "ok"
    assert res["written"] == 4
    assert calc.formulas["C2"] == "=B2*0.08"
    assert calc.formulas["C3"] == "=B3*0.08"
    assert calc.formulas["C4"] == "=B4*0.08"
    assert calc.formulas["C5"] == "=B5*0.08"
    assert calc._grid[2][2] == "=B3*0.08"
    assert calc._grid[3][2] == "=B4*0.08"


def test_calc_json_array_of_identical_formulas_stays_pinned() -> None:
    """JSON array is exact per-cell; repeating =B2*0.08 still pins every row."""
    calc = CalcWorld("Item\tPrice\nApple\t10\nBanana\t5\nOrange\t8\nPear\t12.5")
    pinned = '["=B2*0.08","=B2*0.08","=B2*0.08","=B2*0.08"]'
    res = calc.write_range(range=["C2:C5"], values=pinned)
    assert res["status"] == "ok"
    assert res["written"] == 4
    assert calc.formulas["C2"] == "=B2*0.08"
    assert calc.formulas["C3"] == "=B2*0.08"
    assert calc.formulas["C4"] == "=B2*0.08"
    assert calc.formulas["C5"] == "=B2*0.08"
    assert calc._grid[2][2] == "=B2*0.08"


def test_sheet_summary_includes_all_rows() -> None:
    calc = CalcWorld("Item\tPrice\nApple\t10\nBanana\t5\nOrange\t8\nPear\t12.5\nNote\tn/a\nTotal\t?")
    summary = calc.get_sheet_summary()
    assert summary["row_count"] == 7
    assert len(summary["grid"]) == 7
    assert any("Note" in [str(c) for c in row] for row in summary["grid"])


def test_read_cell_range_matches_grid_to_rows() -> None:
    from plugin.calc.inspector import grid_to_rows

    calc = CalcWorld("X\tY\n1\t2")
    calc.formulas["B2"] = "=A2+1"
    res = calc.read_range(range=["A1:B2"])
    expected = grid_to_rows(calc._inspector_grid("A1:B2"), range_name="A1:B2")
    assert res["status"] == "ok"
    assert "range" not in res
    assert res["result"][0] == {"status": "ok", "range": "A1:B2", "result": [expected]}
    assert res["result"][0]["result"][0]["formulas"] == {"B2": "=A2+1"}


def test_read_cell_range_multi_range_envelope() -> None:
    calc = CalcWorld("a\tb")
    res = calc.read_range(range=["A1", "B1"])
    assert res["status"] == "ok"
    assert "range" not in res
    assert len(res["result"]) == 2
    assert res["result"][0]["status"] == "ok"
    assert res["result"][0]["range"] == "A1"
    assert res["result"][1]["range"] == "B1"


def test_range_sort_respects_range_and_relative_header() -> None:
    """H1: sort_range sorts only the given range; has_header applies to the range's first row."""
    calc = CalcWorld(
        "Product\tRevenue\nWidget\t1200\nGadget\t850\nTool\t2100\nDevice\t1200\nAardvark\tn/a"
    )
    # Data-only range A2:B6 with has_header=False must keep row 1 ("Product") at top.
    res = calc.sort_range(range=["A2:B6"], sort_column=0, ascending=True, has_header=False)
    assert res["status"] == "ok"
    assert calc._grid[0] == ["Product", "Revenue"]
    assert [row[0] for row in calc._grid[1:]] == ["Aardvark", "Device", "Gadget", "Tool", "Widget"]

    # Range A2:B6 with has_header=True treats row 2 ("Aardvark") as header and sorts rows 3..6.
    res2 = calc.sort_range(range=["A2:B6"], sort_column=1, ascending=False, has_header=True)
    assert res2["status"] == "ok"
    assert calc._grid[0] == ["Product", "Revenue"]
    assert calc._grid[1][0] == "Aardvark"
    assert [row[0] for row in calc._grid[2:]] == ["Tool", "Device", "Widget", "Gadget"]


def test_range_sort_subcolumns_leave_outside_columns_untouched() -> None:
    """H1: sorting B1:C4 only reorders columns B and C, and sort_column is 0-based in range."""
    calc = CalcWorld("Keep\tItem\tVal\nr1\tB\t20\nr2\tA\t10\nr3\tC\t30")
    res = calc.sort_range(range=["B1:C4"], sort_column=1, ascending=True, has_header=True)
    assert res["status"] == "ok"
    assert [row[0] for row in calc._grid] == ["Keep", "r1", "r2", "r3"]
    assert [row[1:] for row in calc._grid] == [
        ["Item", "Val"],
        ["A", "10"],
        ["B", "20"],
        ["C", "30"],
    ]


def test_range_sort_invalid_sort_column_errors() -> None:
    calc = CalcWorld("A\tB\n1\t2\n3\t4")
    res = calc.sort_range(range=["A1:B3"], sort_column=2, has_header=True)
    assert res["status"] == "error"
    assert "sort_column must be an integer from 0 to 1" in res["message"]


def test_range_sort_reports_no_rows_moved() -> None:
    """H4: unchanged key column reports the production 'no rows moved' message."""
    calc = CalcWorld("Item\tVal\nA\t10\nB\t20\nC\t30")
    res = calc.sort_range(range=["A1:B4"], sort_column=1, ascending=True, has_header=True)
    assert res["status"] == "ok"
    assert res["message"] == _sort_unchanged_message("A1:B4", [(1, True)])

    res_moved = calc.sort_range(range=["A1:B4"], sort_column=1, ascending=False, has_header=True)
    assert res_moved["status"] == "ok"
    assert res_moved["message"] == "Range A1:B4 sorted descending by column 1."


def test_range_sort_then_by_one_call() -> None:
    calc = CalcWorld(
        "Product\tRevenue\n"
        "Widget\t1200\n"
        "Gadget\t850\n"
        "Tool\t2100\n"
        "Device\t1200\n"
        "Aardvark\tn/a"
    )
    res = calc.sort_range(
        range=["A1:B6"],
        sort_column=1,
        ascending=False,
        then_by=[{"column": 0, "ascending": True}],
        has_header=True,
    )
    assert res["status"] == "ok"
    assert [row[0] for row in calc._grid[1:]] == ["Aardvark", "Tool", "Device", "Widget", "Gadget"]


def test_range_sort_then_by_invalid_leaves_grid() -> None:
    calc = CalcWorld("Product\tRevenue\nWidget\t1200\nDevice\t1200")
    before = [list(r) for r in calc._grid]

    bad_item = calc.sort_range(
        range=["A1:B3"],
        sort_column=1,
        ascending=False,
        then_by=[{"column": 0}],
        has_header=True,
    )
    assert bad_item["status"] == "error"
    assert calc._grid == before

    out_of_width = calc.sort_range(
        range=["A1:B3"],
        sort_column=1,
        ascending=False,
        then_by=[{"column": 2, "ascending": True}],
        has_header=True,
    )
    assert out_of_width["status"] == "error"
    assert "then_by column" in out_of_width["message"]
    assert calc._grid == before


def test_write_formula_range_flattens_2d_array_and_rejects_mismatch() -> None:
    """H2: 2-D arrays fill row-by-row; shape mismatch returns an error without mutating."""
    calc = CalcWorld("Product\tRevenue\nWidget\t1200\nGadget\t850")
    res = calc.write_range(
        range=["A2:B3"],
        values=[["Tool", 2100], ["Device", 1200]],
    )
    assert res["status"] == "ok"
    assert res["written"] == 4
    assert calc._grid[1] == ["Tool", 2100]
    assert calc._grid[2] == ["Device", 1200]

    bad = calc.write_range(range=["A2:B4"], values=[["X", 1], ["Y", 2]])
    assert bad["status"] == "error"
    assert "2x2" in bad["message"]
    assert "3x2" in bad["message"]
    assert calc._grid[1] == ["Tool", 2100]


def test_write_formula_range_expands_2d_from_single_cell() -> None:
    calc = CalcWorld("A\tB")
    res = calc.write_range(range=["A2"], values=[["x", 1], ["y", 2]])
    assert res["status"] == "ok"
    assert res["written"] == 4
    assert calc._grid[1] == ["x", 1]
    assert calc._grid[2] == ["y", 2]


def test_write_formula_range_does_not_json_parse_py_formula_with_brackets() -> None:
    """H2b: =PY(...) containing [...] must not be split into a list."""
    calc = CalcWorld("Product\tRevenue\nWidget\t1200")
    formula = '=PY("result = df[\'Revenue\'].sum()"; A1:B2)'
    res = calc.write_range(range=["C2"], values=formula)
    assert res["status"] == "ok"
    assert res["written"] == 1
    assert calc.formulas["C2"] == formula
    assert calc._grid[1][2] == formula


def test_parse_a1_range_sheet_qualified_and_range_sort() -> None:
    assert parse_a1_range("Sheet1!A1:B6") == (0, 0, 1, 5)
    assert parse_a1_range("'My Sheet'!A1:B6") == (0, 0, 1, 5)
    assert parse_a1_range("'Q1.Sales'!B2:C4") == (1, 1, 2, 3)

    calc = CalcWorld("Item\tVal\nB\t20\nA\t10\nC\t30")
    res = calc.sort_range(range=["'My Sheet'!A1:B4"], sort_column=1, ascending=True, has_header=True)
    assert res["status"] == "ok"
    assert "sorted ascending" in res["message"]
    assert [row[0] for row in calc._grid[1:]] == ["A", "B", "C"]


def test_range_sort_mixed_ascending() -> None:
    calc = CalcWorld("Item\tVal\nalpha\tbeta\nnum10\t10\nempty\t\nnum2\t2\napple\tapple")
    res = calc.sort_range(range=["A1:B6"], sort_column=1, ascending=True, has_header=True)
    assert res["status"] == "ok"
    assert [row[1] for row in calc._grid[1:]] == ["2", "10", "apple", "beta", ""]


def test_range_sort_mixed_descending_with_empties() -> None:
    calc = CalcWorld("Item\tVal\nnum2\t2\nempty1\t\napple\tapple\nnum10\t10\nbeta\tbeta\nempty2\t")
    res = calc.sort_range(range=["A1:B7"], sort_column=1, ascending=False, has_header=True)
    assert res["status"] == "ok"
    assert [row[1] for row in calc._grid[1:]] == ["beta", "apple", "10", "2", "", ""]
    assert [row[0] for row in calc._grid[1:]] == ["beta", "apple", "num10", "num2", "empty1", "empty2"]


def test_write_range_refuses_delimited_text() -> None:
    calc = CalcWorld("A\tB")
    before = [list(row) for row in calc._grid]
    res = calc.write_range(range=["A2"], values="x,1\ny,2")
    assert res["status"] == "error"
    assert calc._grid == before
