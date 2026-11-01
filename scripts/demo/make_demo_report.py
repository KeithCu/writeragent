#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the demo Writer report for the WriterAgent Enterprises video.

Numbers come from ``get_sales_dataset()`` in the showcase workbook generator,
so the report and ``python_showcase_demo.ods`` always agree. The palette is
the workbook's. The text is *deliberately* rough (typos, a rambling section)
so the grammar checker and the "make it shorter" prompt have visible work.

Grammar demo: the Executive Summary is clean except "Each of the regions have
exceeded their quota" (subject-verb agreement; harper-ls does not flag it, an
LLM checker does). The live-typed line "Revenue rose 12% while costs fell
faster, so our margins shrank." is a logic error only an LLM catches; it is
typed on camera, not stored in the report.

Usage: python scripts/demo/make_demo_report.py [--out-dir DIR]
Writes the report, two sibling docs and ``Sales_Data.ods`` (HTML -> soffice convert).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from generate_pretty_demo_spreadsheet import PALETTE as P, get_sales_dataset  # noqa: E402

FONT = "'Segoe UI','Liberation Sans',Arial,sans-serif"
REGIONS = ["North", "South", "East", "West"]
CATS = ["Electronics", "Furniture", "Supplies"]


def _totals():
    rows = get_sales_dataset()[1:]  # row 0 is the header
    by_r, by_c = defaultdict(float), defaultdict(float)
    for r in rows:
        by_r[r[2]] += r[7]
        by_c[r[3]] += r[7]
    return by_r, by_c, sum(by_r.values()), len(rows)


def _table(head: list[str], body: list[list[str]]) -> str:
    th = "".join(
        f'<td style="background:#{P["table_header_bg"]};color:#{P["table_header_fg"]};'
        f'padding:5pt;font-weight:bold">{h}</td>' for h in head)
    trs = [f"<tr>{th}</tr>"]
    for i, row in enumerate(body):
        bg = P["zebra_even"] if i % 2 == 0 else P["zebra_odd"]
        tds = "".join(
            f'<td style="background:#{bg};color:#{P["text_dark"]};padding:5pt;'
            f'border-bottom:0.5pt solid #{P["card_border"]}">{c}</td>' for c in row)
        trs.append(f"<tr>{tds}</tr>")
    return f'<table width="100%" cellspacing="0">{"".join(trs)}</table>'


def build_html() -> str:
    by_r, by_c, total, n = _totals()
    reg_rows = [[r, f"${by_r[r]:,.0f}", f"{by_r[r] / total:.0%}"] for r in REGIONS]
    cat_rows = [[c, f"${by_c[c]:,.0f}", f"{by_c[c] / total:.0%}"] for c in CATS]
    best = max(by_r, key=by_r.get)
    h2 = f'font-family:{FONT};font-size:14pt;color:#{P["accent_blue"]};margin-top:14pt;margin-bottom:4pt'
    body = f'font-size:10.5pt;color:#{P["text_dark"]};line-height:140%'
    return f"""<html><head><meta charset="utf-8"><style>
body{{font-family:{FONT}}}</style></head><body>
<table width="100%" cellspacing="0"><tr><td style="background:#{P["hero_bg"]};padding:16pt">
<p style="font-size:22pt;font-weight:bold;color:#{P["hero_fg"]};margin:0">WriterAgent Enterprises</p>
<p style="font-size:11pt;color:#94A3B8;margin:2pt 0 0 0">Quarterly Business Review &middot; January&ndash;May 2024</p>
</td></tr></table>

<h2 style="{h2}">Executive Summary</h2>
<p style="{body}">There was a lot of good news this quarter. Total revenue reached
<b>${total:,.0f}</b> across {n} orders, and the {best} region led the way. Enterprise
customers are our fastest growing segment, and we received more large orders than
ever before. Each of the regions have exceeded their quota, and the team deserves
credit for their hard work.</p>

<h2 style="{h2}">Regional Performance</h2>
{_table(["Region", "Revenue", "Share"], reg_rows)}

<h2 style="{h2}">Product Categories</h2>
<p style="{body}">In terms of the various product categories that we sell, it is
worth noting at this point in time that Electronics has continued to be the
single largest category by revenue, which is something that we have seen in
each of the previous reporting periods as well, and which we expect will most
likely continue to be the case going forward into the future. Furniture, on
the other hand, has had somewhat of a mixed performance, with some very large
orders from Enterprise customers in the West and South regions but also a
number of smaller orders from Consumer customers that did not contribute
very much to the overall total. Supplies, finally, are a high volume but low
margin category, and while they account for a large number of units sold,
they make up only a small portion of the total revenue figure.</p>
{_table(["Category", "Revenue", "Share"], cat_rows)}

<h2 style="{h2}">Outlook</h2>
<p style="{body}">We expect continued growth in the second half of the year.
The full order data lives in the accompanying workbook.</p>
</body></html>"""


def _simple_doc(title: str, subtitle: str, paras: list[str]) -> str:
    """Small sibling doc with the same hero banner, for the document-research scene."""
    ps = "".join(
        f'<p style="font-family:{FONT};font-size:10.5pt;color:#{P["text_dark"]};line-height:140%">{t}</p>'
        for t in paras)
    return (f'<html><head><meta charset="utf-8"></head><body>'
            f'<table width="100%" cellspacing="0"><tr><td style="background:#{P["hero_bg"]};padding:14pt">'
            f'<p style="font-family:{FONT};font-size:18pt;font-weight:bold;color:#{P["hero_fg"]};margin:0">{title}</p>'
            f'<p style="font-family:{FONT};font-size:10pt;color:#94A3B8;margin:2pt 0 0 0">{subtitle}</p>'
            f'</td></tr></table>{ps}</body></html>')


# Siblings the document_research / cross-file search scene finds. Facts are
# distinctive on purpose ("90 days", "Pacific Components") so the answer is checkable on camera.
SIBLINGS = {
    "West_Supplier_Contract": _simple_doc(
        "Supplier Agreement &mdash; Pacific Components", "WriterAgent Enterprises &middot; West region electronics", [
            "Pacific Components supplies electronics inventory for the West region under a 24-month term.",
            "Volume discount: 8% on any single order above 500 units.",
            "Renewal: either party must give 90 days written notice before the term ends, "
            "otherwise the agreement renews automatically for 12 months.",
        ]),
    "Board_Meeting_Notes": _simple_doc(
        "Board Meeting Notes", "WriterAgent Enterprises &middot; April 2024", [
            "Decision: renew the Pacific Components supplier agreement before the end of June.",
            "Decision: open a second West office and hire two sales engineers for Enterprise accounts.",
            "Action: finance to confirm Furniture pricing for Consumer customers in the South.",
        ]),
}


def _convert(html: str, name: str, out: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"{name}.html"
        src.write_text(html, encoding="utf-8")
        # writer8 filter forces Writer (not Writer/Web) so styles survive as .odt
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "odt:writer8", "--infilter=HTML (StarWriter)",
             "--outdir", str(out), str(src)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "build" / "demo"))
    out = Path(ap.parse_args().out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _convert(build_html(), "WriterAgent_Enterprises_Q2_Review", out)
    for name, html in SIBLINGS.items():
        _convert(html, name, out)
    # The sales workbook sits beside the report: it is the Calc peer for the
    # Writer <-> Calc scene and a document-research hit.
    wb = ROOT / "tests" / "fixtures" / "python_showcase_demo.ods"
    shutil.copy(wb, out / "Sales_Data.ods")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
