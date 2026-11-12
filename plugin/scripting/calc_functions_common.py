# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Row and column caps for the tables the venv helper domains return.

``analysis``, ``forecast``, ``optimize`` and ``duckdb_sql`` (all under
``plugin/scripting/venv/``) import these. No ``calc_functions`` module reads them.
"""
from __future__ import annotations

# --- Scripting Domain Constants ---

ANALYSIS_MAX_TABLE_ROWS = 50
# Cleaned sheets, pivots, decompositions and SQL results are the user's data, so they get a
# larger cap than summary tables (ANALYSIS_MAX_TABLE_ROWS). 200 rows still bounds what one
# result sends back to the sheet or the chat.
ANALYSIS_DATA_TABLE_ROWS = 200
ANALYSIS_MAX_COLS = 40

