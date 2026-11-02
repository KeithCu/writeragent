# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared calc_functions constants — importable without numpy on the LO host."""
from __future__ import annotations

# --- Scripting Domain Constants ---

ANALYSIS_MAX_TABLE_ROWS = 50
# Cleaned sheets, pivots, and decompositions are the user's data, not an LLM
# summary. 50 rows used to drop the rest with only a truncated flag the sheet
# egress buried. 200 matches the DuckDB result cap and still bounds a chat reply.
ANALYSIS_DATA_TABLE_ROWS = 200
ANALYSIS_MAX_COLS = 40

