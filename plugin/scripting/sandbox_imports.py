# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Curated venv sandbox import whitelist (shared by venv_sandbox and import_policy)."""

from __future__ import annotations

# Mirror plugin/contrib/smolagents/utils.py BASE_BUILTIN_MODULES (keep in sync).
BASE_BUILTIN_MODULES: tuple[str, ...] = (
    "collections",
    "datetime",
    "itertools",
    "math",
    "queue",
    "random",
    "re",
    "stat",
    "statistics",
    "time",
    "unicodedata",
)

# Mirror plugin/contrib/smolagents/local_python_executor.py DANGEROUS_MODULES (keep in sync).
DANGEROUS_MODULES: tuple[str, ...] = (
    "builtins",
    "io",
    "multiprocessing",
    "os",
    "pathlib",
    "pty",
    "shutil",
    "socket",
    "subprocess",
    "sys",
)

# Curated by WriterAgent (see docs/enabling_numpy_in_libreoffice.md)—not "whatever is in the venv".
VENV_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    "platform",
    "numpy",
    "numpy.*",
    "pandas",
    "pandas.*",
    "scipy",
    "scipy.*",
    "sklearn",
    "sklearn.*",
    "matplotlib",
    "matplotlib.*",
    "seaborn",
    "seaborn.*",
    "sympy",
    "sympy.*",
    "statsmodels",
    "statsmodels.*",
    "networkx",
    "networkx.*",
    "PIL",
    "PIL.*",
    "data_profiling",
    "data_profiling.*",
    "pandas_montecarlo",
    "pandas_montecarlo.*",
    "cv2",
    "json",
    "csv",
    "decimal",
    "fractions",
    "functools",
    "operator",
    "string",
    "textwrap",
    "enum",
    "dataclasses",
    "typing",
    "copy",
    "pprint",
    "webview",
    "jedi",
    "PyQt6",
    "PyQt6.QtWebEngineWidgets",
    "qtpy",
    "plugin.scripting.payload_codec",
    "plugin.scripting.embeddings_index",
    "plugin.scripting.embeddings_chroma",
    "plugin.scripting.embeddings_ingest_graph",
    "plugin.scripting.embeddings_search_graph",
    "plugin.scripting.analysis",
    "plugin.scripting.analysis_coerce",
    "plugin.scripting.vision",
    "plugin.scripting.vision_common",
    "plugin.scripting.vision_docling",
    "plugin.scripting.vision_paddle",
    "plugin.scripting.vision_html_export",
    "css_inline",
    "plugin.scripting.viz",
    "plugin.scripting.viz_common",
    "plugin.scripting.symbolic",
    "plugin.scripting.symbolic_common",
    "sentence_transformers",
    "sentence_transformers.*",
    "yfinance",
    "yfinance.*",
    "pandas_ta",
    "pandas_ta.*",
    "quantstats",
    "quantstats.*",
    "pypfopt",
    "pypfopt.*",
    "plugin.scripting.quant",
    "plugin.scripting.quant_common",
    "plugin.scripting.optimize",
    "plugin.scripting.optimize_common",
    "plugin.scripting.calc_functions",
    "plugin.scripting.calc_functions.*",
)

# In-process LO embedded sandbox (execute_python_script) — stdlib-only extras beyond BASE_BUILTIN_MODULES.
CALC_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    "math",
    "datetime",
    "random",
    "json",
    "re",
    "collections",
    "itertools",
    "statistics",
)
