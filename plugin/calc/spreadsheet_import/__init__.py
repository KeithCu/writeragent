# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc spreadsheet → Python import pipeline; import from the submodules.

Stage order (orchestrated by ``pipeline.run_sheet_conversion``):
1. ``ingest`` (snapshot into ``SheetModel``, ``graph`` circular groups)
2. ``preserve.enrich_number_formats`` (optional)
3. ``emit.build_converted_output_model`` (``vectorize`` runs, ``translate``, ``range_clip``, compose ``=PY()``, report)
4. target sheet (new ``PythonImport`` or source range cleared)
5. ``preserve.apply_output_to_sheet``
6. ``verify`` after recalc (optional)

Supporting modules: ``models`` (shared dataclasses and ``ReportCode``), ``preprocess`` (formula normalization), and ``import_dialog`` (options UI).
"""
