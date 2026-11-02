"""Contracts sidecar for plugin.scripting.sandbox."""

from __future__ import annotations

import deal

from plugin.scripting import sandbox as _prod
from tests.contracts.bounds import (
    DEAL_MAX_ARGV,
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_PATH,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    inverse_ensure,
    str_bounded,
)

_DEAL_SCRUB_DICT = 1 if UNDER_CROSSHAIR else DEAL_MAX_ARGV
_DEAL_SCRUB_KEY = 2 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN
_DEAL_SCRUB_VAL = 4 if UNDER_CROSSHAIR else DEAL_MAX_ARGV


def _deal_scrub_env_ok_pytest(base: object) -> bool:
    if base is None:
        return True
    return isinstance(base, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in base.items())


def _deal_scrub_env_ok_crosshair(base: object) -> bool:
    return base is None or (
        isinstance(base, dict)
        and len(base) <= _DEAL_SCRUB_DICT
        and all(
            isinstance(k, str)
            and isinstance(v, str)
            and ascii_bounded(k, _DEAL_SCRUB_KEY)
            and ascii_bounded(v, _DEAL_SCRUB_VAL)
            for k, v in base.items()
        )
    )


_deal_scrub_env_ok = _deal_scrub_env_ok_crosshair if UNDER_CROSSHAIR else _deal_scrub_env_ok_pytest


def _deal_path_ok_pytest(path: object) -> bool:
    return isinstance(path, str)


def _deal_path_ok_crosshair(path: object) -> bool:
    return str_bounded(path, DEAL_MAX_PATH)


_deal_path_ok = _deal_path_ok_crosshair if UNDER_CROSSHAIR else _deal_path_ok_pytest

_DEAL_BASENAME_LEN = 8 if UNDER_CROSSHAIR else DEAL_MAX_PATH

scrub_subprocess_env = deal.pre(lambda base: _deal_scrub_env_ok(base))(
    deal.post(lambda result: isinstance(result, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in result.items()))(
        inverse_ensure(lambda base, result: all(k.upper() not in _prod._BLOCKED_ENV_EXACT for k in result))(
            inverse_ensure(lambda base, result: all(not _prod._env_name_is_credential(k) for k in result))(
                deal.ensure(
                    lambda base, result: base is None
                    or (
                        result.get("PYTHONIOENCODING") == "utf-8"
                        and result.get("PYTHONUTF8") == "1"
                        and result.get("PYTHONDONTWRITEBYTECODE") == "1"
                    )
                )(_prod.scrub_subprocess_env)
            )
        )
    )
)

wrap_command_for_sandbox = deal.pre(
    lambda cmd, env=None, cwd=None, watch_bus=False: isinstance(cmd, list)
    and len(cmd) <= DEAL_MAX_CMD_ARGS
    and all(str_bounded(x, DEAL_MAX_ARGV) for x in cmd)
    and (
        env is None
        or (
            isinstance(env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        )
    )
    and (cwd is None or isinstance(cwd, str))
)(
    deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))(
        deal.ensure(
            lambda cmd, env=None, cwd=None, watch_bus=False, result=None: isinstance(result, list)
            and len(result) >= len(cmd)
            and (result[-len(cmd):] == cmd if cmd else True)
        )(_prod.wrap_command_for_sandbox)
    )
)

_is_acceptable_python_basename = deal.pre(lambda base: ascii_bounded(base, _DEAL_BASENAME_LEN))(
    _prod._is_acceptable_python_basename
)

PYTEST = [
    "scrub_subprocess_env",
    "wrap_command_for_sandbox",
    "_is_acceptable_python_basename",
]

TARGETS = [f"{__name__}.{name}" for name in PYTEST]
