# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.duckdb_sql."""

from __future__ import annotations


import deal

from plugin.scripting import duckdb_sql as _prod
from tests.contracts.bounds import DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, str_bounded


def _deal_sql_code_ok_pytest(code: object) -> bool:
    return str_bounded(code, DEAL_MAX_SOURCE)


def _deal_sql_code_ok_crosshair(code: object) -> bool:
    return isinstance(code, str) and len(code) <= DEAL_MAX_SOURCE


_deal_sql_code_ok = _deal_sql_code_ok_crosshair if UNDER_CROSSHAIR else _deal_sql_code_ok_pytest

_DEAL_SQL_RESULT_KEYS = 4 if UNDER_CROSSHAIR else 32
_DEAL_SQL_RESULT_STR = 8 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN


def _deal_sql_result_value_ok_pytest(value: object) -> bool:
    if isinstance(value, dict):
        return True
    return False


def _deal_sql_result_value_ok_crosshair(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if len(value) > _DEAL_SQL_RESULT_KEYS:
        return False
    for k, v in value.items():
        if not isinstance(k, str) or not str_bounded(k, _DEAL_SQL_RESULT_STR):
            return False
        if isinstance(v, str) and not str_bounded(v, _DEAL_SQL_RESULT_STR):
            return False
    return True


_deal_sql_result_value_ok = (
    _deal_sql_result_value_ok_crosshair if UNDER_CROSSHAIR else _deal_sql_result_value_ok_pytest
)

get_sql_script_templates = deal.post(
    lambda result: isinstance(result, dict) and "query_folder_sql" in result and "query_sheet_sql" in result
)(_prod.get_sql_script_templates)

parse_sql_script_header = deal.post(
    lambda result: result is None or isinstance(result, _prod.HelperScriptMeta)
)(
    deal.pre(lambda code: _deal_sql_code_ok(code))(_prod.parse_sql_script_header)
)

is_sql_result = deal.pre(lambda value: _deal_sql_result_value_ok(value))(_prod.is_sql_result)

PYTEST = ["get_sql_script_templates", "parse_sql_script_header", "is_sql_result"]
TARGETS = [f"{__name__}.is_sql_result"]
