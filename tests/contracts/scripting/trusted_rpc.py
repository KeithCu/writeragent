"""Contracts sidecar for plugin.scripting.trusted_rpc."""

from __future__ import annotations

import deal

from plugin.framework.errors import ToolExecutionError
from plugin.scripting import trusted_rpc as _prod
from tests.contracts.bounds import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN, str_bounded

parse_worker_dict_result = deal.pre(
    lambda response, error_code="", error_label="": isinstance(response, dict)
    and len(response) <= DEAL_MAX_SHAPE_DIM
    and str_bounded(error_code, DEAL_MAX_TOKEN)
    and str_bounded(error_label, DEAL_MAX_TOKEN)
)(
    deal.post(lambda result: isinstance(result, dict))(
        deal.raises(ToolExecutionError)(
            _prod.parse_worker_dict_result
        )
    )
)

PYTEST = [
    "parse_worker_dict_result",
]

TARGETS = [f"{__name__}.{name}" for name in PYTEST]
