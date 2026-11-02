# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.helper_domain."""

from __future__ import annotations

import deal

from plugin.scripting import helper_domain as _prod
from tests.contracts.bounds import (
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    str_bounded,
)


def _deal_user_code_ok_pytest(code: object, run_name: object = "") -> bool:
    return str_bounded(code, DEAL_MAX_SOURCE) and ascii_bounded(run_name, DEAL_MAX_TOKEN)


def _deal_user_code_ok_crosshair(code: object, run_name: object = "") -> bool:
    return (
        isinstance(code, str)
        and len(code) <= DEAL_MAX_SOURCE
        and ascii_bounded(run_name, DEAL_MAX_TOKEN)
    )


_deal_user_code_ok = _deal_user_code_ok_crosshair if UNDER_CROSSHAIR else _deal_user_code_ok_pytest

header_prefix = deal.post(
    lambda result: isinstance(result, str) and result.startswith("# writeragent:")
)(
    deal.pre(lambda tag: str_bounded(tag, DEAL_MAX_TOKEN, min_len=1))(_prod.header_prefix)
)

parse_helper_script_header = deal.post(
    lambda result: result is None or isinstance(result, _prod.HelperScriptMeta)
)(_prod.parse_helper_script_header)

parse_run_import_call_spec = deal.post(lambda result: result is None or isinstance(result, dict))(
    deal.pre(lambda code, run_name="": _deal_user_code_ok(code, run_name))(
        _prod.parse_run_import_call_spec
    )
)

PYTEST = [
    "header_prefix",
    "parse_helper_script_header",
    "parse_run_import_call_spec",
]
TARGETS = [f"{__name__}.header_prefix"]
