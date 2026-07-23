#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI entry: Excel ``xl()`` / ``=PY`` ↔ DAG-style ``=PY(code; ranges)``.

See ``python -m plugin.calc.excel_py_convert --help``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugin.calc.excel_py_convert.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
