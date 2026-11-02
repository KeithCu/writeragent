"""Contracts sidecar for plugin.calc.calc_addin_data."""

from __future__ import annotations

import deal

from plugin.calc import calc_addin_data as _prod

finalize_python_data = deal.post(
    lambda result: result is None or isinstance(result, list)
)(_prod.finalize_python_data)

calc_addin_data_to_python = deal.post(
    lambda result: result is None or isinstance(result, list)
)(_prod.calc_addin_data_to_python)

PYTEST = [
    "finalize_python_data",
    "calc_addin_data_to_python",
]

TARGETS: list[str] = []
