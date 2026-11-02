# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.stream_delta."""

from __future__ import annotations

import deal

from plugin.framework import stream_delta as _prod

accumulate_delta = deal.raises(TypeError, RuntimeError)(
    deal.post(lambda result: isinstance(result, dict))(
        deal.pre(lambda acc, delta: type(acc) is dict and type(delta) is dict)(
            _prod.accumulate_delta
        )
    )
)

PYTEST = ["accumulate_delta"]
TARGETS: list[str] = []
