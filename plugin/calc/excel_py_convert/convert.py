# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel Python-in-Excel ↔ DAG-style ``=PY`` conversion: offline stages and the udprop meta.

No single caller runs every stage. The four pipelines:

================  ==========================================  ==============================================
                  from a file (CLI, ``cli.py``)               from the live document (``auto_open.py``)
================  ==========================================  ==============================================
Excel → ``=PY``   ``convert_path`` → ``convert_to_dag``       open: ``has_excel_python_xlsx`` →
                  (``load_excel_model`` → ``convert_model_    ``convert_to_dag`` → ``apply_dag_formulas_to_
                  to_dag``) → ``write_dag_formulas_xlsx``     calc_doc`` → ``store_dag_meta_on_doc``
``=PY`` → Excel   ``convert_path`` → ``convert_to_excel``     save: ``convert_uno_doc_to_excel`` (reads
                  (``iter_dag_py_formulas_xlsx`` →            ``ExcelPyDagMeta``) → ``write_excel_python_
                  ``convert_dag_cells_to_excel``, or          xlsx`` → ``has_excel_python_xlsx`` check
                  ``--from-report``) →
                  ``write_excel_python_xlsx``
================  ==========================================  ==============================================

Per-cell rewrite rules are in ``to_dag.py`` and ``to_excel.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from plugin.calc.excel_py_convert.models import ConversionReport, DagCellMeta, DagFormulaItem
from plugin.calc.excel_py_convert.parse_dag_formulas import iter_dag_py_formulas_xlsx
from plugin.calc.excel_py_convert.parse_excel_ooxml import load_excel_model
from plugin.calc.excel_py_convert.script_bank import add_safety_warnings
from plugin.calc.excel_py_convert.to_dag import convert_model_to_dag
from plugin.calc.excel_py_convert.to_excel import (
    convert_dag_cells_to_excel,
    convert_dag_report_to_excel,
)
from plugin.framework.json_utils import safe_json_loads

log = logging.getLogger(__name__)

# Udprop: per-cell JSON so auto-save can restore return_type / data_args / excel_deps.
EXCEL_PY_DAG_META_PROP = "ExcelPyDagMeta"


def convert_to_dag(path: str | Path, *, best_effort: bool = False) -> ConversionReport:
    """Excel XLSX or JSON fixture → DAG-style conversion report.

    The sandbox advisories are added to each converted cell's issues; ``ok`` ignores them.
    """
    model = load_excel_model(path)
    report = convert_model_to_dag(model, best_effort=best_effort)
    add_safety_warnings(report)
    return report


def convert_to_excel(path: str | Path) -> ConversionReport:
    """Workbook with DAG ``=PY`` formulas → Excel report (``xl(%Pn%)`` + script indices).

    Pair with ``write_excel_python_xlsx`` to emit native ``pythonScripts.xml`` /
    ``_xlfn._xlws.PY``. Resolves ``py_code_*`` bank refs when reading ``.xlsx``.

    For fidelity (return_type / data_args), prefer ``convert_dag_report_to_excel`` on
    an in-memory DAG report, or pass cell meta via udprop ``ExcelPyDagMeta``.
    """
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("direction") == "dag":
            return convert_dag_report_to_excel(ConversionReport.from_dict(data))
        return convert_dag_cells_to_excel(_items_from_json(data), report_meta=data if isinstance(data, dict) else None)
    items = iter_dag_py_formulas_xlsx(path)
    report = convert_dag_cells_to_excel(items)
    report.source_path = str(path)
    return report


def cell_meta_key(sheet: str, cell: str) -> str:
    return f"{sheet}!{cell}"


def dag_report_to_meta_payload(report: ConversionReport) -> dict[str, Any]:
    """Compact per-cell meta for udprop (auto-save / CLI reverse), read by ``DagCellMeta``.

    Includes ``excel_deps`` for Table/ANCHORARRAY export fidelity — see models module doc.
    """
    return {cell_meta_key(c.sheet, c.cell): c.dag_meta() for c in report.cells if c.converted}


def store_dag_meta_on_doc(doc: Any, report: ConversionReport) -> None:
    """Persist DAG conversion meta on *doc* for later native Excel export on save."""
    # udprops imports uno; the command-line converter loads this module without LibreOffice.
    from plugin.doc.udprops import set_document_property

    payload = dag_report_to_meta_payload(report)
    set_document_property(doc, EXCEL_PY_DAG_META_PROP, json.dumps(payload, separators=(",", ":")))


def load_dag_meta_from_doc(doc: Any) -> dict[str, Any]:
    """Load ``ExcelPyDagMeta`` JSON map, or ``{}``."""
    # udprops imports uno; the command-line converter loads this module without LibreOffice.
    from plugin.doc.udprops import get_document_property

    raw = get_document_property(doc, EXCEL_PY_DAG_META_PROP)
    if not raw:
        return {}
    data = safe_json_loads(raw, default={})
    return data if isinstance(data, dict) else {}


def _items_from_json(data: Any) -> list[DagFormulaItem]:
    if isinstance(data, dict) and "cells" in data:
        out: list[DagFormulaItem] = []
        for c in data["cells"]:
            formula = c.get("dag_formula") or c.get("formula")
            if not formula:
                continue
            out.append(DagFormulaItem(str(c.get("sheet", "Sheet1")), str(c.get("cell", "A1")), str(formula), DagCellMeta.from_dict(c)))
        return out
    if isinstance(data, list):
        return [DagFormulaItem(str(x["sheet"]), str(x["cell"]), str(x["formula"]), DagCellMeta.from_dict(x)) for x in data]
    raise ValueError("JSON must be a dag report or list of {sheet, cell, formula}")


def convert_path(path: str | Path, *, direction: str, out_report: str | Path | None = None, best_effort: bool = False, from_report: str | Path | None = None) -> ConversionReport:
    """Convert *path* in *direction* ``dag`` or ``excel``; optionally write JSON report.

    When ``direction == "excel"`` and *from_report* is a DAG conversion JSON,
    use ``convert_dag_report_to_excel`` (preserves return_type / data_args).
    """
    direction = direction.strip().lower()
    if direction == "dag":
        report = convert_to_dag(path, best_effort=best_effort)
    elif direction == "excel":
        if from_report is not None:
            data = json.loads(Path(from_report).read_text(encoding="utf-8"))
            report = convert_dag_report_to_excel(ConversionReport.from_dict(data))
            report.source_path = report.source_path or str(path)
        else:
            report = convert_to_excel(path)
    else:
        raise ValueError("direction must be 'dag' or 'excel'")
    if out_report is not None:
        Path(out_report).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report
