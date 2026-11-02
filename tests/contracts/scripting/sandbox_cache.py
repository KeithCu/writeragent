"""Contracts sidecar for plugin.scripting.sandbox_cache."""

from __future__ import annotations

import deal

from plugin.scripting import sandbox_cache as _prod
from tests.contracts.bounds import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    _profile,
    ascii_bounded,
    str_bounded,
)


def _deal_sandbox_imports_ok_pytest(authorized_imports: object) -> bool:
    if not isinstance(authorized_imports, (list, tuple)):
        return False
    return all(isinstance(x, str) and "\0" not in x and str_bounded(x, DEAL_MAX_TOKEN) for x in authorized_imports)


def _deal_sandbox_imports_ok_crosshair(authorized_imports: object) -> bool:
    if not isinstance(authorized_imports, (list, tuple)):
        return False
    return (
        len(authorized_imports) <= DEAL_MAX_CMD_ARGS
        and all(ascii_bounded(x, DEAL_MAX_TOKEN) for x in authorized_imports)
    )


_deal_sandbox_imports_ok = _profile(_deal_sandbox_imports_ok_crosshair, _deal_sandbox_imports_ok_pytest)


def _deal_sandbox_code_ok_pytest(code: object) -> bool:
    return isinstance(code, str) and "\0" not in code


def _deal_sandbox_code_ok_crosshair(code: object) -> bool:
    return isinstance(code, str) and ascii_bounded(code, DEAL_MAX_SOURCE) and "\0" not in code


_deal_sandbox_code_ok = _profile(_deal_sandbox_code_ok_crosshair, _deal_sandbox_code_ok_pytest)


def _deal_sandbox_policy_ok(authorized_imports: object, blocked_imports: object = None) -> bool:
    if not _deal_sandbox_imports_ok(authorized_imports):
        return False
    if blocked_imports is None:
        return True
    if not isinstance(blocked_imports, (list, tuple, set, frozenset)):
        return False
    return all(isinstance(x, str) and "\0" not in x and str_bounded(x, DEAL_MAX_TOKEN) for x in blocked_imports)


validate_sandbox_ast = deal.pre(
    lambda module, authorized_imports, blocked_imports=None: _deal_sandbox_policy_ok(authorized_imports, blocked_imports)
)(
    deal.post(lambda result: result is None or isinstance(result, str))(
        _prod.validate_sandbox_ast
    )
)

get_hot_entry = deal.pre(
    lambda code, authorized_imports: _deal_sandbox_policy_ok(authorized_imports) and _deal_sandbox_code_ok(code)
)(_prod.get_hot_entry)

PYTEST = [
    "validate_sandbox_ast",
    "get_hot_entry",
]

TARGETS: list[str] = []
