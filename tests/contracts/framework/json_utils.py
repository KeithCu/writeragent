# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.json_utils."""

from __future__ import annotations

import deal

from plugin.framework import json_utils as _prod

safe_json_loads = deal.ensure(
    lambda text, default=None, strict=False, result=None: not (
        isinstance(text, str) and text.strip() == ""
    )
    or result is default
)(
    deal.ensure(
        lambda text, default=None, strict=False, result=None: isinstance(
            text, (str, bytes, bytearray)
        )
        or result is default
    )(_prod.safe_json_loads)
)

PYTEST = ["safe_json_loads"]
TARGETS: list[str] = []
