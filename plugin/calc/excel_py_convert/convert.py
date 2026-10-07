# SPDX-License-Identifier: GPL-3.0-or-later
"""Orchestrate Excel ↔ DAG-style ``=PY`` conversion. Details in ``to_dag.py``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from plugin.calc.excel_py_convert.models import ConversionReport
from plugin.calc.excel_py_convert.parse_dag_formulas import iter_dag_py_formulas_xlsx
from plugin.calc.excel_py_convert.parse_excel_ooxml import load_excel_model
from plugin.calc.excel_py_convert.to_dag import convert_model_to_dag
from plugin.calc.excel_py_convert.to_excel import (
    convert_dag_cells_to_excel,
    convert_dag_report_to_excel,
)
from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel
from plugin.calc.excel_py_convert.xlsx_write import (
    _clear_spill_range,
    _clear_spill_xml,
    _ensure_cell,
    _patch_content_types,
    _set_cell_xlws_formula,
    _strip_content_types_python,
    _strip_python_in_excel_parts,
    _strip_rels_python,
    non_anchor_spill_cells,
    write_dag_formulas_xlsx,
    write_excel_python_xlsx,
)

log = logging.getLogger(__name__)

# Udprop: per-cell JSON so auto-save can restore return_type / data_args / excel_deps.
EXCEL_PY_DAG_META_PROP = "ExcelPyDagMeta"

__all__ = [
    "EXCEL_PY_DAG_META_PROP",
    "cell_meta_key",
    "convert_path",
    "convert_to_dag",
    "convert_to_excel",
    "convert_uno_doc_to_excel",
    "dag_report_to_meta_payload",
    "load_dag_meta_from_doc",
    "non_anchor_spill_cells",
    "store_dag_meta_on_doc",
    "write_dag_formulas_xlsx",
    "write_excel_python_xlsx",
    "_clear_spill_range",
    "_clear_spill_xml",
    "_ensure_cell",
    "_patch_content_types",
    "_set_cell_xlws_formula",
    "_strip_content_types_python",
    "_strip_python_in_excel_parts",
    "_strip_rels_python",
]


def convert_to_dag(path: str | Path, *, best_effort: bool = False) -> ConversionReport:
    """Excel XLSX or JSON fixture → DAG-style conversion report."""
    model = load_excel_model(path)
    return convert_model_to_dag(model, best_effort=best_effort)


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
        return convert_dag_cells_to_excel(_triples_from_json(data), report_meta=data if isinstance(data, dict) else None)
    items = iter_dag_py_formulas_xlsx(path)
    report = convert_dag_cells_to_excel(items)
    report.source_path = str(path)
    return report


def cell_meta_key(sheet: str, cell: str) -> str:
    return f"{sheet}!{cell}"


def dag_report_to_meta_payload(report: ConversionReport) -> dict[str, Any]:
    """Compact per-cell meta for udprop (auto-save / CLI reverse).

    Includes ``excel_deps`` for Table/ANCHORARRAY export fidelity — see models module doc.
    """
    out: dict[str, Any] = {}
    for c in report.cells:
        if not c.converted:
            continue
        out[cell_meta_key(c.sheet, c.cell)] = {
            "return_type": int(c.return_type or 0),
            "data_args": list(c.data_args),
            "excel_deps": list(c.excel_deps),
            "ordering_args": list(c.ordering_args),
            "array_ref": c.array_ref,
            "bindings": [{"a1": b.a1, "header_mode": b.header_mode, "role": b.role, "original_indices": list(b.original_indices)} for b in c.bindings],
        }
    return out


def store_dag_meta_on_doc(doc: Any, report: ConversionReport) -> None:
    """Persist DAG conversion meta on *doc* for later native Excel export on save."""
    from plugin.doc.udprops import set_document_property

    payload = dag_report_to_meta_payload(report)
    set_document_property(doc, EXCEL_PY_DAG_META_PROP, json.dumps(payload, separators=(",", ":")))


def load_dag_meta_from_doc(doc: Any) -> dict[str, Any]:
    """Load ``ExcelPyDagMeta`` JSON map, or ``{}``."""
    from plugin.doc.udprops import get_document_property

    raw = get_document_property(doc, EXCEL_PY_DAG_META_PROP)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _triples_from_json(data: Any) -> list[tuple[str, str, str, dict[str, Any]]]:
    if isinstance(data, dict) and "cells" in data:
        out: list[tuple[str, str, str, dict[str, Any]]] = []
        for c in data["cells"]:
            formula = c.get("dag_formula") or c.get("formula")
            if not formula:
                continue
            out.append((str(c.get("sheet", "Sheet1")), str(c.get("cell", "A1")), str(formula), dict(c)))
        return out
    if isinstance(data, list):
        return [(str(x["sheet"]), str(x["cell"]), str(x["formula"]), dict(x)) for x in data]
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
