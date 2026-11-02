"""Contracts sidecar for plugin.calc.python.xl_static_rewrite."""

from __future__ import annotations

import deal

from plugin.calc.python import xl_static_rewrite as _prod
from plugin.calc.python.xl_static_rewrite import XlStaticRewriteResult
from tests.contracts.bounds import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SOURCE, str_bounded

apply_xl_static_rewrite = deal.pre(
    lambda code, existing_data_args=None: str_bounded(code, DEAL_MAX_SOURCE)
    and (
        existing_data_args is None
        or (
            isinstance(existing_data_args, list)
            and len(existing_data_args) <= DEAL_MAX_SHAPE_DIM
            and all(str_bounded(x, DEAL_MAX_SOURCE) for x in existing_data_args)
        )
    )
)(
    deal.post(
        lambda result: isinstance(result, XlStaticRewriteResult)
        and isinstance(result.code, str)
        and isinstance(result.data_args, list)
    )(_prod.apply_xl_static_rewrite)
)

PYTEST = [
    "apply_xl_static_rewrite",
]

TARGETS: list[str] = []
