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

from plugin.framework.deal_shim import deal


def _deal_model_id_ok(*args: object, **kwargs: object) -> bool:
    return True


def _deal_catalog_ids_ok(*args: object, **kwargs: object) -> bool:
    return True

# Dynamic: routing/behavior shortcuts (any model; strip for catalog lookup).
OPENROUTER_DYNAMIC_SUFFIXES = frozenset({"nitro", "floor", "exacto", "online"})


@deal.pre(lambda model_id: _deal_model_id_ok(model_id))
@deal.post(lambda result: isinstance(result, str))
def _split_suffix(model_id: str) -> str:
    parts = model_id.split(":")
    if any(not p for p in parts):
        return model_id
    kept = [parts[0]] + [p for p in parts[1:] if p not in OPENROUTER_DYNAMIC_SUFFIXES]
    return ":".join(kept)


@deal.pre(lambda model_id, catalog_ids=None: _deal_model_id_ok(model_id) and _deal_catalog_ids_ok(catalog_ids))
@deal.post(lambda result: isinstance(result, str))
def resolve_openrouter_catalog_id(model_id: str, catalog_ids: Iterable[str] | None = None) -> str:
    """Return the catalog key to use for capabilities/metadata lookup.

    Exact match wins (static variants like ``:free``). Dynamic suffixes
    (``:nitro``, ``:floor``, …) fall back to the base slug.
    """
    mid = str(model_id or "").strip()
    if catalog_ids is not None and mid in catalog_ids:
        return mid
    base = _split_suffix(mid)
    if base != mid and (catalog_ids is None or base in catalog_ids):
        return base
    return mid


@deal.pre(lambda a, b: _deal_model_id_ok(a) and _deal_model_id_ok(b))
@deal.post(lambda result: isinstance(result, bool))
def openrouter_model_ids_equivalent(a: str, b: str) -> bool:
    """True if two OpenRouter ids refer to the same underlying catalog model."""
    # Leave _split_suffix and resolve_openrouter_catalog_id on for check-all.
    # crosshair: off
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if not sa or not sb:
        return sa == sb
    if sa == sb:
        return True
    ra = resolve_openrouter_catalog_id(sa)
    rb = resolve_openrouter_catalog_id(sb)
    if ra == rb:
        return True
    # Tip: 'gpt-oss-20b:nitro' (no vendor) must still match 'openai/gpt-oss-20b'.
    if ("/" in ra) != ("/" in rb):
        return ra.split("/", 1)[-1] == rb.split("/", 1)[-1]
    return False
