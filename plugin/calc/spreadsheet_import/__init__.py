# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc spreadsheet → Python import: ingest, translate, emit, preserve."""

from plugin.calc.spreadsheet_import.extract import (
    canonicalize_py_formula_for_parse,
    extract_py_cells,
    is_py_formula_text,
    normalize_py_formula,
    py_formula_semantics,
)
from plugin.calc.spreadsheet_import.emit import build_converted_output_model, emit_py_formula
from plugin.calc.spreadsheet_import.graph import (
    attach_graph_to_model,
    build_dependency_graph,
    extract_cell_refs,
    extract_range_refs,
    topological_formula_order,
)
from plugin.calc.spreadsheet_import.ingest import classify_cell, ingest_from_arrays, ingest_sheet
from plugin.calc.spreadsheet_import.models import (
    FORMULA_LIKE_TYPES,
    CellRecord,
    CellType,
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    PyCellExtract,
    SheetModel,
    TranslationResult,
    VerifyResult,
)
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.spreadsheet_import.preserve import (
    apply_output_to_sheet,
    build_output_model,
    enrich_number_formats,
    preserve_sheet_to_new_sheet,
)
from plugin.calc.spreadsheet_import.translate import translate_formula
from plugin.calc.spreadsheet_import.verify import verify_converted_cells

__all__ = [
    "FORMULA_LIKE_TYPES",
    "CellRecord",
    "CellType",
    "OutputCell",
    "OutputSheetModel",
    "PyCellExtract",
    "SheetModel",
    "apply_output_to_sheet",
    "attach_graph_to_model",
    "build_dependency_graph",
    "build_converted_output_model",
    "build_output_model",
    "canonicalize_py_formula_for_parse",
    "ConversionReport",
    "classify_cell",
    "enrich_number_formats",
    "emit_py_formula",
    "extract_cell_refs",
    "extract_py_cells",
    "extract_range_refs",
    "ingest_from_arrays",
    "is_py_formula_text",
    "ingest_sheet",
    "normalize_lo_formula_for_parse",
    "normalize_py_formula",
    "preserve_sheet_to_new_sheet",
    "py_formula_semantics",
    "topological_formula_order",
    "translate_formula",
    "TranslationResult",
    "verify_converted_cells",
    "VerifyResult",
]
