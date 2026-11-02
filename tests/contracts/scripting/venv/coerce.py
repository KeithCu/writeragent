"""Contracts sidecar for plugin.scripting.venv.coerce."""

from __future__ import annotations

import deal

from plugin.scripting.venv import coerce as _prod

_dedupe_column_names = deal.post(
    lambda result: isinstance(result, list) and len(set(result)) == len(result)
)(_prod._dedupe_column_names)

PYTEST = [
    "_dedupe_column_names",
]

TARGETS = [f"{__name__}.{name}" for name in PYTEST]
