# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.calc.excel_py_convert.cli."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from plugin.calc.excel_py_convert.cli import main


def _dag_xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1"><f>PY(&quot;df = data.to_pandas()&quot;,B1:C2)</f></c>
      <c r="B1"><v>1</v></c>
    </row>
  </sheetData>
</worksheet>"""
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_cli_missing_input(tmp_path: Path, capsys):
    rc = main([str(tmp_path / "nope.xlsx"), "--to", "dag"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err


def test_cli_bad_zip(tmp_path: Path, capsys):
    bad = tmp_path / "corrupt.xlsx"
    bad.write_bytes(b"not a zip")
    rc = main([str(bad), "--to", "dag"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err


def test_cli_write_failure(tmp_path: Path, capsys):
    src = tmp_path / "dag.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    missing_dir = tmp_path / "missing_dir"
    missing_dir.write_text("block")
    out = missing_dir / "out.xlsx"
    rc = main([str(src), "--to", "excel", "--write-xlsx", str(out)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err


def test_cli_usage_from_report_without_excel(tmp_path: Path):
    src = tmp_path / "dag.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    rc = main([str(src), "--to", "dag", "--from-report", str(src)])
    assert rc == 2


def test_cli_stdout_cleanliness(tmp_path: Path, capsys):
    src = tmp_path / "dag.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    out = tmp_path / "out.xlsx"
    rc = main([str(src), "--to", "excel", "--write-xlsx", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "cells" in data
    assert "wrote" in captured.err
    assert "wrote" not in captured.out
