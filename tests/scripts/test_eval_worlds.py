# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Worlds for string eval: HTML blocks, Draw edges, Calc dest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_worlds import CalcWorld, DrawWorld, WriterWorld


def test_html_apply_round_trip_heading_and_table() -> None:
    world = WriterWorld("plain")
    html = "<h1>Intro</h1><table><tr><td>A</td></tr></table>"
    world.apply_document_content(target="full_document", content=html)
    types = [b["type"] for b in world.blocks]
    assert "heading" in types
    assert "table" in types
    assert "<h1>Intro</h1>" in world.export_for_prompt()


def test_style_field_from_class() -> None:
    world = WriterWorld("")
    world.apply_document_content(
        target="full_document",
        content='<p class="Quotations">Default style paragraph one.</p>',
    )
    styled = [b for b in world.blocks if b["style"] == "Quotations"]
    assert styled
    assert "Quotations" in world.get_html()


def test_add_comment_on_uncertain() -> None:
    world = WriterWorld("The results are uncertain at this point.")
    res = world.add_comment(search="uncertain", content="Review this before finalizing")
    assert res["comment_added"] is True
    assert "[Review this before finalizing]" in world.get_html()
    commented = [b for b in world.blocks if b["comments"]]
    assert commented


def test_search_in_document_offsets() -> None:
    world = WriterWorld("<p>Hello world</p>")
    found = world.search_in_document(pattern="world")
    assert found["status"] == "ok"
    assert found["ranges"]


def test_draw_connect_tree() -> None:
    draw = DrawWorld()
    draw.shape_upsert(action="create", shape_type="ellipse", text="Start")
    draw.shape_upsert(action="create", shape_type="flowchart-process", text="Process")
    res = draw.shape_connect(start=0, end=1)
    assert res["status"] == "ok"
    tree = draw.get_draw_tree()
    assert tree["connections"]
    nodes = tree["tree"]
    assert any(n.get("connected_end") or n.get("connected_start") for n in nodes)


def test_calc_py_dest_snapshot() -> None:
    calc = CalcWorld("Name\tAmt\nAnn\t1")
    calc.write_formula_range(
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
    assert names == ["Tool", "Device", "Widget", "Gadget", "Aardvark"]


def test_calc_write_formula_range_rejects_length_mismatch() -> None:
    calc = CalcWorld("Item\tAmt\nAnn\t1\nBea\t2\nCal\t3\nDee\t4")
    res = calc.write_formula_range(range=["C2:C9"], values=["a", "b", "c", "d"])
    assert res["status"] == "error"
    assert "4 values" in res["message"]
    assert "8 cells" in res["message"]
    # Must not zip-truncate into the extra rows.
    assert calc.writes == []


def test_calc_write_formula_range_accepts_scalar_fill() -> None:
    calc = CalcWorld("Item\tAmt\nAnn\t1")
    res = calc.write_formula_range(range=["C2:C5"], values="=B2+1")
    assert res["status"] == "ok"
    assert res["written"] == 4


def test_sheet_summary_includes_all_rows() -> None:
    calc = CalcWorld("Item\tPrice\nApple\t10\nBanana\t5\nOrange\t8\nPear\t12.5\nNote\tn/a\nTotal\t?")
    summary = calc.get_sheet_summary()
    assert summary["row_count"] == 7
    assert len(summary["grid"]) == 7
    assert any("Note" in [str(c) for c in row] for row in summary["grid"])
