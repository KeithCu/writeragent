# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.config_schema."""

from __future__ import annotations

import deal

from plugin.framework import config_schema as _prod

as_bool = deal.post(lambda result: isinstance(result, bool))(_prod.as_bool)

parse_int_robust = deal.raises(ValueError)(_prod.parse_int_robust)

parse_float_robust = deal.raises(ValueError)(
    deal.post(lambda result: isinstance(result, float))(_prod.parse_float_robust)
)

PYTEST = ["as_bool", "parse_int_robust", "parse_float_robust"]
TARGETS: list[str] = []
