# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared viz constants — importable without matplotlib on the LO host."""
from __future__ import annotations

HELPER_NAMES = frozenset(
    {
        "quick_plot",
        "plot_data",
        "correlation_heatmap",
        "time_series_plot",
    }
)

VIZ_VENV_PIP_INSTALL = "pip install matplotlib seaborn"
