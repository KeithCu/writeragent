"""Contracts sidecar for plugin.calc.sheet_filter_criteria."""

from __future__ import annotations

import deal

from plugin.calc import sheet_filter_criteria as _prod
from plugin.calc.sheet_filter_criteria import FILTER_OPERATOR2_LABELS, FilterCriterion
from plugin.framework.errors import UnoObjectError
from tests.contracts.bounds import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded, profile


def _deal_filter_op_name_ok_pytest(name: object) -> bool:
    return isinstance(name, str)


def _deal_filter_op_name_ok_crosshair(name: object) -> bool:
    return isinstance(name, str) and name in FILTER_OPERATOR2_LABELS


_deal_filter_op_name_ok = profile(_deal_filter_op_name_ok_crosshair, _deal_filter_op_name_ok_pytest)


def _deal_filter_connection_ok_pytest(name: object) -> bool:
    return name is None or isinstance(name, str)


def _deal_filter_connection_ok_crosshair(name: object) -> bool:
    return name is None or name in ("AND", "OR", "and", "or", "")


_deal_filter_connection_ok = profile(_deal_filter_connection_ok_crosshair, _deal_filter_connection_ok_pytest)


def _deal_filter_criterion_ok_crosshair(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and len(raw) <= DEAL_MAX_CMD_ARGS
        and (not isinstance(raw.get("operator"), str) or ascii_bounded(raw.get("operator"), DEAL_MAX_TOKEN))
        and (not isinstance(raw.get("connection"), str) or ascii_bounded(raw.get("connection"), DEAL_MAX_TOKEN))
    )


_deal_filter_criterion_ok = profile(_deal_filter_criterion_ok_crosshair)

filter_connection_code = deal.pre(lambda name: _deal_filter_connection_ok(name))(
    deal.post(lambda result: result in (0, 1))(
        deal.raises(UnoObjectError)(_prod.filter_connection_code)
    )
)

resolve_filter_operator_code = deal.pre(lambda operator: _deal_filter_op_name_ok(operator))(
    deal.post(lambda result: isinstance(result, int) and result >= 0)(
        deal.raises(UnoObjectError)(_prod.resolve_filter_operator_code)
    )
)

parse_sheet_filter_criterion = deal.pre(lambda raw, is_first: _deal_filter_criterion_ok(raw))(
    deal.post(lambda result: isinstance(result, FilterCriterion))(
        deal.raises(UnoObjectError)(_prod.parse_sheet_filter_criterion)
    )
)

PYTEST = [
    "filter_connection_code",
    "resolve_filter_operator_code",
    "parse_sheet_filter_criterion",
]

TARGETS = [
    f"{__name__}.filter_connection_code",
    f"{__name__}.resolve_filter_operator_code",
]
