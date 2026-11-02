# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.client.auth."""

from __future__ import annotations

import deal

from plugin.framework.client import auth as _prod
from tests.contracts.bounds import DEAL_MAX_TOKEN, UNDER_CROSSHAIR, str_bounded

_PROVIDER_HINTS = frozenset(_prod.PROVIDERS)


def _deal_provider_id_ok_pytest(provider_id: object) -> bool:
    return provider_id is None or str_bounded(provider_id, DEAL_MAX_TOKEN)


def _deal_provider_id_ok_crosshair(provider_id: object) -> bool:
    return provider_id is None or provider_id in _PROVIDER_HINTS


_deal_provider_id_ok = _deal_provider_id_ok_crosshair if UNDER_CROSSHAIR else _deal_provider_id_ok_pytest

provider_requires_api_key = deal.post(lambda result: isinstance(result, bool))(
    deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))(_prod.provider_requires_api_key)
)

provider_requires_slug_model_id = deal.post(lambda result: isinstance(result, bool))(
    deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))(_prod.provider_requires_slug_model_id)
)

PYTEST = ["provider_requires_api_key", "provider_requires_slug_model_id"]
TARGETS = [
    f"{__name__}.provider_requires_api_key",
    f"{__name__}.provider_requires_slug_model_id",
]
