# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.i18n."""

from __future__ import annotations

import deal

from plugin.framework import i18n as _prod
from tests.contracts.bounds import DEAL_MAX_MSGID, str_bounded

get_active_locale = deal.post(lambda result: isinstance(result, str) and len(result) > 0)(
    _prod.get_active_locale
)

_ = deal.post(lambda result: isinstance(result, str))(
    deal.pre(lambda message: str_bounded(message, DEAL_MAX_MSGID))(_prod._)
)

PYTEST = ["get_active_locale", "_"]
TARGETS = [f"{__name__}.get_active_locale"]
