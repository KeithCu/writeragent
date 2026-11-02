"""Contracts sidecar for plugin.calc.address_utils."""

from __future__ import annotations

import deal

from plugin.calc import address_utils as _prod
from tests.contracts.bounds import (
    DEAL_MAX_CELL_REF,
    DEAL_MAX_COL_INDEX,
    DEAL_MAX_COL_LETTERS,
    DEAL_MAX_ROW_INDEX,
    ascii_bounded,
    inverse_ensure,
)

index_to_column = deal.pre(
    lambda index: isinstance(index, int) and 0 <= index <= DEAL_MAX_COL_INDEX
)(
    deal.post(
        lambda result: isinstance(result, str) and 1 <= len(result) <= DEAL_MAX_COL_LETTERS
    )(_prod.index_to_column)
)

column_to_index = deal.pre(
    lambda col_str: ascii_bounded(col_str, DEAL_MAX_COL_LETTERS, min_len=1) and col_str.isalpha()
)(
    deal.post(lambda result: isinstance(result, int) and result >= 0)(
        inverse_ensure(lambda col_str, result: _prod.index_to_column(result) == col_str.upper())(
            _prod.column_to_index
        )
    )
)

parse_address = deal.pre(lambda address: ascii_bounded(address, DEAL_MAX_CELL_REF, min_len=1))(
    deal.post(
        lambda result: isinstance(result, tuple)
        and len(result) == 2
        and result[0] >= 0
        and result[1] >= 0
    )(
        deal.raises(ValueError)(_prod.parse_address)
    )
)

parse_range_string = deal.pre(lambda range_str: ascii_bounded(range_str, DEAL_MAX_CELL_REF, min_len=1))(
    deal.post(
        lambda result: isinstance(result, tuple)
        and len(result) == 2
        and all(isinstance(p, tuple) and len(p) == 2 and p[0] >= 0 and p[1] >= 0 for p in result)
    )(
        deal.raises(ValueError)(_prod.parse_range_string)
    )
)

format_address = deal.pre(
    lambda col, row: isinstance(col, int)
    and 0 <= col <= DEAL_MAX_COL_INDEX
    and isinstance(row, int)
    and 0 <= row <= DEAL_MAX_ROW_INDEX
)(
    deal.post(
        lambda result: isinstance(result, str)
        and bool(result)
        and result[0].isalpha()
        and result.isascii()
        and result[-1].isdigit()
    )(
        inverse_ensure(lambda col, row, result: _prod.parse_address(result) == (col, row))(
            _prod.format_address
        )
    )
)

PYTEST = [
    "index_to_column",
    "column_to_index",
    "parse_address",
    "parse_range_string",
    "format_address",
]

TARGETS = [
    f"{__name__}.column_to_index",
    f"{__name__}.format_address",
]
