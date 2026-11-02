# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.tool."""

from __future__ import annotations

import deal

from plugin.framework import tool as _prod
from tests.contracts.bounds import DEAL_MAX_TOKEN, ascii_bounded

_name_looks_readonly = deal.pre(
    lambda name: isinstance(name, str) and ascii_bounded(name, DEAL_MAX_TOKEN)
)(_prod._name_looks_readonly)

PYTEST = ["_name_looks_readonly"]
TARGETS = [f"{__name__}._name_looks_readonly"]
