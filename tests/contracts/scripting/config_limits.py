# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.config_limits."""

from __future__ import annotations

import deal

from plugin.scripting import config_limits as _prod

_clamp = deal.ensure(
    lambda field_name, value, result: isinstance(result, int)
    and _prod._bounds(field_name)[0] <= result <= _prod._bounds(field_name)[1]
)(_prod._clamp)

resolve_python_exec_timeout = deal.post(
    lambda result: isinstance(result, int)
    and _prod._bounds("python_exec_timeout")[0] <= result <= _prod._bounds("python_exec_timeout")[1]
)(_prod.resolve_python_exec_timeout)

PYTEST = ["_clamp", "resolve_python_exec_timeout"]
TARGETS = [f"{__name__}._clamp", f"{__name__}.resolve_python_exec_timeout"]
