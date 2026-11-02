# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.excel_xl."""

from __future__ import annotations

import deal

from plugin.scripting import excel_xl as _prod
from tests.contracts.bounds import DEAL_MAX_SHAPE_DIM

make_xl = deal.pre(
    lambda ranges: ranges is None
    or (isinstance(ranges, (list, tuple)) and len(ranges) <= DEAL_MAX_SHAPE_DIM)
)(_prod.make_xl)

PYTEST = ["make_xl"]
TARGETS = [f"{__name__}.make_xl"]
