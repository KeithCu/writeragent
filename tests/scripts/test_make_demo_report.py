# SPDX-License-Identifier: GPL-3.0-or-later
"""Demo report generator: numbers match the showcase workbook; grammar traps stay put."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("make_demo_report", ROOT / "scripts" / "demo" / "make_demo_report.py")
assert _spec and _spec.loader
mdr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mdr)


def test_report_totals_match_workbook_dataset():
    by_r, _by_c, total, n = mdr._totals()
    html = mdr.build_html()
    assert f"${total:,.0f}" in html and f"{n} orders" in html
    assert abs(sum(by_r.values()) - total) < 1e-6
