# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.constants."""

from __future__ import annotations

import datetime as _dt
import deal

from plugin.framework import constants as _prod

now_aware = deal.post(lambda result: isinstance(result, _dt.datetime) and result.tzinfo is not None)(
    _prod.now_aware
)

PYTEST = ["now_aware"]
TARGETS = [f"{__name__}.now_aware"]
