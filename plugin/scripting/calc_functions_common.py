# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared calc_functions constants — importable without numpy on the LO host."""
from __future__ import annotations

# --- Scripting Domain Constants ---

ANALYSIS_MAX_TABLE_ROWS = 50
# Cleaned sheets, pivots, and decompositions are the user's data, not an LLM
# Cap analysis tables at 200 rows so large sheet egress stays bounded like DuckDB limits and chat summaries stay readable.
ANALYSIS_DATA_TABLE_ROWS = 200
ANALYSIS_MAX_COLS = 40

