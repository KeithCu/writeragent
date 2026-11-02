# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.openrouter_model_id."""

from __future__ import annotations

import deal

from plugin.framework import openrouter_model_id as _prod
from tests.contracts.bounds import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN, profile, str_bounded

_deal_model_id_ok = profile(lambda model_id: str_bounded(model_id, DEAL_MAX_TOKEN))
_deal_catalog_ids_ok = profile(
    lambda ids: ids is None
    or (
        isinstance(ids, (set, frozenset, list, tuple))
        and len(ids) <= DEAL_MAX_SHAPE_DIM
        and all(str_bounded(x, DEAL_MAX_TOKEN) for x in ids)
    )
)

_split_suffix = deal.post(lambda result: isinstance(result, str))(
    deal.pre(lambda model_id: _deal_model_id_ok(model_id))(_prod._split_suffix)
)

resolve_openrouter_catalog_id = deal.post(lambda result: isinstance(result, str))(
    deal.pre(
        lambda model_id, catalog_ids=None: _deal_model_id_ok(model_id) and _deal_catalog_ids_ok(catalog_ids)
    )(_prod.resolve_openrouter_catalog_id)
)

openrouter_model_ids_equivalent = deal.post(lambda result: isinstance(result, bool))(
    deal.pre(lambda a, b: _deal_model_id_ok(a) and _deal_model_id_ok(b))(
        _prod.openrouter_model_ids_equivalent
    )
)

PYTEST = [
    "_split_suffix",
    "resolve_openrouter_catalog_id",
    "openrouter_model_ids_equivalent",
]
TARGETS = [
    f"{__name__}._split_suffix",
    f"{__name__}.resolve_openrouter_catalog_id",
]
