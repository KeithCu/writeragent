# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared ODS fixtures for embeddings / FTS tests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_budget_ods(path: Path) -> None:
    """Minimal Budget.ods with one data row for extract and FTS tests."""
    frame = pd.DataFrame([["Q4", "Revenue", 1_200_000]])
    frame.to_excel(path, engine="odf", sheet_name="Budget", header=False, index=False)
