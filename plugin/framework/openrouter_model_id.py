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

# Dynamic: routing/behavior shortcuts (any model; strip for catalog lookup).
OPENROUTER_DYNAMIC_SUFFIXES = frozenset({"nitro", "floor", "exacto", "online"})

# Static: separate catalog rows when listed for a model (exact match only).
OPENROUTER_STATIC_SUFFIXES = frozenset({"free", "extended", "thinking"})


def _split_suffix(model_id: str) -> tuple[str, str | None]:
    if ":" not in model_id:
        return model_id, None
    base, suffix = model_id.rsplit(":", 1)
    if not base or not suffix:
        return model_id, None
    return base, suffix


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


def openrouter_model_ids_equivalent(a: str, b: str, catalog_ids: Iterable[str] | None = None) -> bool:
    """True if two OpenRouter ids refer to the same underlying catalog model."""
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if not sa or not sb:
        return sa == sb
    if sa == sb:
        return True
    return resolve_openrouter_catalog_id(sa, catalog_ids) == resolve_openrouter_catalog_id(sb, catalog_ids)
