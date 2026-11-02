# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.default_models."""

from __future__ import annotations

import deal

from plugin.framework import default_models as _prod
from tests.contracts.bounds import DEAL_MAX_TOKEN, str_bounded

resolve_model_id = deal.post(lambda result: result is None or isinstance(result, str))(
    _prod.resolve_model_id
)

get_provider_defaults = deal.post(lambda result: isinstance(result, dict))(
    deal.pre(lambda provider: not provider or str_bounded(provider, DEAL_MAX_TOKEN))(
        _prod.get_provider_defaults
    )
)

PYTEST = ["resolve_model_id", "get_provider_defaults"]
TARGETS = [f"{__name__}.get_provider_defaults"]
