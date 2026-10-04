# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenRouter model id suffixes (:nitro, :floor, :free, …).

Dynamic suffixes apply to any model and are not separate Orca /v1/models rows.
Static suffixes (:free, :extended, :thinking) are model-specific catalog ids.
See https://openrouter.ai/docs/faq (Models and Providers → Variants).
"""

from __future__ import annotations

from typing import Iterable

from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN, UNDER_CROSSHAIR, str_bounded, deal


def _deal_model_id_ok_pytest(model_id: object) -> bool:
    # Catalog ids are longer than DEAL_MAX_TOKEN. The cap raised
    # PreContractError on a real OpenRouter id. The body splits on ':'.
    return isinstance(model_id, str)


def _deal_model_id_ok_crosshair(model_id: object) -> bool:
    return str_bounded(model_id, DEAL_MAX_TOKEN)


_deal_model_id_ok = _deal_model_id_ok_crosshair if UNDER_CROSSHAIR else _deal_model_id_ok_pytest


def _deal_catalog_ids_ok_pytest(catalog_ids: object) -> bool:
    # The live catalog has more than DEAL_MAX_SHAPE_DIM models.
    return catalog_ids is None or isinstance(catalog_ids, (list, tuple, set, frozenset))


def _deal_catalog_ids_ok_crosshair(catalog_ids: object) -> bool:
    return catalog_ids is None or (
        isinstance(catalog_ids, (list, tuple, set, frozenset)) and len(catalog_ids) <= DEAL_MAX_SHAPE_DIM
    )


_deal_catalog_ids_ok = _deal_catalog_ids_ok_crosshair if UNDER_CROSSHAIR else _deal_catalog_ids_ok_pytest

# Dynamic: routing/behavior shortcuts (any model; strip for catalog lookup).
OPENROUTER_DYNAMIC_SUFFIXES = frozenset({"nitro", "floor", "exacto", "online"})

# Static: separate catalog rows when listed for a model (exact match only).
OPENROUTER_STATIC_SUFFIXES = frozenset({"free", "extended", "thinking"})


@deal.pre(lambda model_id: _deal_model_id_ok(model_id))
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2)
def _split_suffix(model_id: str) -> tuple[str, str | None]:
    if ":" not in model_id:
        return model_id, None
    base, suffix = model_id.rsplit(":", 1)
    if not base or not suffix:
        return model_id, None
    return base, suffix


@deal.pre(lambda model_id, catalog_ids=None: _deal_model_id_ok(model_id) and _deal_catalog_ids_ok(catalog_ids))
@deal.post(lambda result: isinstance(result, str))
def resolve_openrouter_catalog_id(model_id: str, catalog_ids: Iterable[str] | None = None) -> str:
    """Return the catalog key to use for capabilities/metadata lookup.

    Exact match wins (static variants like ``:free``). Dynamic suffixes
    (``:nitro``, ``:floor``, …) fall back to the base slug.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return mid
    catalog = set(catalog_ids) if catalog_ids is not None else None
    if catalog is not None and mid in catalog:
        return mid
    base, suffix = _split_suffix(mid)
    if suffix in OPENROUTER_DYNAMIC_SUFFIXES:
        if catalog is None or base in catalog:
            return base
    return mid


@deal.pre(
    lambda a, b, catalog_ids=None: _deal_model_id_ok(a) and _deal_model_id_ok(b) and _deal_catalog_ids_ok(catalog_ids)
)
@deal.post(lambda result: isinstance(result, bool))
def openrouter_model_ids_equivalent(a: str, b: str, catalog_ids: Iterable[str] | None = None) -> bool:
    """True if two OpenRouter ids refer to the same underlying catalog model."""
    # Deep check-all run 32840960268: Prev 11:29. Leave _split_suffix and
    # resolve_openrouter_catalog_id on (they finished in ~2 min).
    # crosshair: off
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if not sa or not sb:
        return sa == sb
    if sa == sb:
        return True
    return resolve_openrouter_catalog_id(sa, catalog_ids) == resolve_openrouter_catalog_id(sb, catalog_ids)
