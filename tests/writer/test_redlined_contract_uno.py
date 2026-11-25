# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for the synthetic redlined contract fixture and opt-in read benchmark with ceilings."""

from __future__ import annotations

import os
import tempfile
import unittest

import uno
from com.sun.star.beans import PropertyValue

from plugin.doc.text_helpers import redline_bounds
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory
from tests.writer import redlined_contract_bench as bench
from tests.writer.redlined_contract_fixture import (
    AUTHOR,
    DELETED,
    INSERTED,
    TABLE_COLS,
    TABLE_ROWS,
    build_redlined_contract,
)

# Median seconds allowed on the 301-page redlined contract: three to five times the numbers in
# docs/writer/redline-timings.md, so only a change in complexity fails, not a slower machine.
MEDIAN_CEILINGS_S = {
    "get_content full": 15.0,
    "search, 20 of many hits": 3.0,
    "get_tree heading_only": 2.0,
    "get_tree default": 2.0,
    "table_get_cells": 0.4,
}


def _write(folder: str, name: str, xml: str) -> str:
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(xml)
    return uno.systemPathToFileUrl(path)


@native_test
def test_redlined_contract_fixture_loads_uno(ctx):
    contract = build_redlined_contract(articles=4, sections=2, clauses=8, tables=2, multi_every=40)
    with tempfile.TemporaryDirectory() as folder:
        url = _write(folder, "contract.fodt", contract.xml)
        desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        hidden = PropertyValue()
        hidden.Name, hidden.Value = "Hidden", True
        doc = desktop.loadComponentFromURL(url, "_blank", 0, (hidden,))
        assert doc is not None
        try:
            redlines = doc.getRedlines()
            deletes = inserts = multi_hits = table_redlines = 0
            for idx in range(redlines.getCount()):
                rl = redlines.getByIndex(idx)
                assert rl.getPropertyValue("RedlineAuthor") == AUTHOR
                kind = rl.getPropertyValue("RedlineType")
                if kind == "Delete":
                    deletes += 1
                elif kind == "Insert":
                    inserts += 1
                if rl.getPropertyValue("RedlineStart").getPropertyValue("TextTable") is not None:
                    table_redlines += 1
                text, start, end = redline_bounds(rl)
                cursor = text.createTextCursorByRange(start)
                cursor.gotoRange(end, True)
                span = cursor.getString()
                if "whole struck clause" in span:
                    multi_hits += 1
                    assert span.count("\n") == 2

            assert deletes == contract.deletions == 20
            assert inserts == contract.insertions == 18
            assert multi_hits == contract.multi_paragraph_deletions == 2
            assert table_redlines == 2 * 5 * len(contract.table_names)

            tables = doc.getTextTables()
            assert tuple(tables.getElementNames()) == contract.table_names
            first_table = tables.getByName(contract.table_names[0])
            assert len(first_table.getCellNames()) == TABLE_ROWS * TABLE_COLS - 3 - 1
            assert doc.getDocumentIndexes().getCount() == 1

            clause3 = section2 = None
            paras = doc.getText().createEnumeration()
            while enum_has_more(paras):
                para = paras.nextElement()
                if not para.supportsService("com.sun.star.text.Paragraph"):
                    continue
                text = para.getString()
                if section2 is None and "Section 2 Terms" in text:
                    section2 = para
                if clause3 is None and "Clause 3:" in text:
                    clause3 = para
                if section2 is not None and clause3 is not None:
                    break
            assert section2 is not None
            assert section2.getPropertyValue("ListLabelString") == "1.2"
            assert clause3 is not None
            assert int(clause3.getPropertyValue("ParaAdjust")) == 2
            assert clause3.getPropertyValue("ListLabelString") == "(c)"

            result = TestingFactory.execute_tool(doc, ctx, "get_content", doc_type="writer")
            assert result.get("status") == "ok"
            assert not result.get("warning")
            content = result["content"]
            assert DELETED.strip() not in content
            assert "whole struck clause" not in content
            assert INSERTED.strip() in content
        finally:
            doc.close(True)


@native_test(timeout=900)
def test_redlined_contract_read_timings_uno(ctx):
    if os.environ.get("WRITERAGENT_BENCH") != "1":
        raise unittest.SkipTest("set WRITERAGENT_BENCH=1")

    all_ops = [op[0] for op in bench.OPS]
    specs = [
        ("redlined", build_redlined_contract(), all_ops, 3),
        ("no changes", build_redlined_contract(redline_every=0), all_ops, 3),
        ("redlined, one list style", build_redlined_contract(shared_list=True), ["get_content full"], 1),
    ]
    with tempfile.TemporaryDirectory() as folder:
        cases = []
        for idx, (name, contract, ops, repeats) in enumerate(specs):
            url = _write(folder, f"case_{idx}.fodt", contract.xml)
            cases.append({"name": name, "url": url, "ops": ops, "repeats": repeats})
        results = bench.run_in_soffice(ctx, cases)

    print("\n| Document | Pages | Changes | Read | First (s) | Median (s) | Hits | Warning |")
    print("|---|---|---|---|---|---|---|---|")
    for res in results:
        for row in res["timings"]:
            hits = "" if row["count"] is None else str(row["count"])
            warn = row["warning"] or ""
            print(
                f"| {res['name']} | {res['pages']} | {res['redlines']} | {row['op']} "
                f"| {row['first']:.3f} | {row['median']:.3f} | {hits} | {warn} |"
            )

    assert results[0]["pages"] >= 250
    for res in results[:2]:
        full = next(row for row in res["timings"] if row["op"] == "get_content full")
        assert not full["warning"]
    medians = {row["op"]: row["median"] for row in results[0]["timings"]}
    slow = {op: (medians[op], limit) for op, limit in MEDIAN_CEILINGS_S.items() if medians[op] > limit}
    assert not slow, f"median seconds over the ceiling: {slow}"
