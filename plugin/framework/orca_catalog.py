# WriterAgent - Orca / OpenRouter catalog normalization (build-time + runtime helpers)
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize Orca preview API models into slim records and map to ModelCapability."""

from __future__ import annotations

from typing import Any

from plugin.framework.types import ModelCapability

DEFAULT_ORCA_MODELS_URL = "https://orca.orb.town/api/preview/v2/models"


def _parse_price_scalar(v: Any) -> float | None:
    """Parse Orca/OpenRouter-style price fields (strings or numbers)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def aggregate_pricing(providers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Average numeric fields across each provider's ``pricing`` map.

    Nested ``tiers`` lists are omitted (shape varies); only top-level scalar rates are merged.
    For each key, the mean is taken over providers that supply a parseable number for that key.
    """
    pricing_dicts: list[dict[str, Any]] = []
    for p in providers:
        pr = p.get("pricing")
        if isinstance(pr, dict):
            pricing_dicts.append(pr)
    if not pricing_dicts:
        return None
    keys: set[str] = set()
    for pr in pricing_dicts:
        keys.update(pr.keys())
    keys.discard("tiers")
    out: dict[str, Any] = {}
    for k in sorted(keys):
        values: list[float] = []
        for pr in pricing_dicts:
            if k not in pr:
                continue
            val = _parse_price_scalar(pr.get(k))
            if val is not None:
                values.append(val)
        if not values:
            out[k] = None
        else:
            avg = sum(values) / len(values)
            out[k] = round(avg, 12)
    if not any(v is not None for v in out.values()):
        return None
    return out


def aggregate_providers(providers: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Merge all provider rows into one aggregate (max context, union params, any-flags)."""
    if not providers:
        return {
            "context_length": None,
            "supported_parameters": [],
            "chat_completions": None,
            "native_web_search": None,
            "pricing": None,
        }
    ctx_max: int | None = None
    params: set[str] = set()
    chat_any: bool | None = None
    web_any: bool | None = None
    for p in providers:
        cl = p.get("context_length")
        if cl is not None:
            try:
                n = int(cl)
            except (TypeError, ValueError):
                pass
            else:
                ctx_max = n if ctx_max is None else max(ctx_max, n)
        sp = p.get("supported_parameters")
        if isinstance(sp, list):
            for x in sp:
                if isinstance(x, str):
                    params.add(x)
        cc = p.get("chat_completions")
        if isinstance(cc, bool):
            chat_any = cc if chat_any is None else (chat_any or cc)
        nw = p.get("native_web_search")
        if isinstance(nw, bool):
            web_any = nw if web_any is None else (web_any or nw)
    pricing = aggregate_pricing(providers)
    return {
        "context_length": ctx_max,
        "supported_parameters": sorted(params),
        "chat_completions": chat_any,
        "native_web_search": web_any,
        "pricing": pricing,
    }


def normalize_orca_model(raw: dict[str, Any]) -> dict[str, Any]:
    """One slim record per model; providers[] merged into scalars/lists only."""
    agg = aggregate_providers(raw.get("providers") if isinstance(raw.get("providers"), list) else None)
    return {
        "id": raw["id"],
        "name": raw.get("name"),
        "author_name": raw.get("author_name"),
        "input_modalities": list(raw.get("input_modalities") or []),
        "output_modalities": list(raw.get("output_modalities") or []),
        "reasoning": raw.get("reasoning"),
        "variant": raw.get("variant"),
        "version_id": raw.get("version_id"),
        "created_at": raw.get("created_at"),
        "context_length": agg["context_length"],
        "supported_parameters": agg["supported_parameters"],
        "chat_completions": agg["chat_completions"],
        "native_web_search": agg["native_web_search"],
        "pricing": agg["pricing"],
    }


def slim_record_supports_tool_calling(slim: dict[str, Any]) -> bool:
    """True if merged ``supported_parameters`` includes OpenAI-style ``tools`` (tool calling)."""
    params = slim.get("supported_parameters")
    return isinstance(params, list) and "tools" in params


def filter_slim_catalog_tool_calling_only(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop models that do not support tool calling (no ``tools`` in ``supported_parameters``)."""
    models = payload.get("models")
    if not isinstance(models, list):
        return payload
    kept = [m for m in models if isinstance(m, dict) and slim_record_supports_tool_calling(m)]
    newp = dict(payload)
    newp["models"] = kept
    return newp


def slim_catalog_payload(
    raw_api: dict[str, Any],
    *,
    source_url: str,
    generator: str = "orca_openrouter_catalog",
) -> dict[str, Any]:
    """Build the JSON object written by the sync script (tool-calling models only)."""
    models_raw = raw_api.get("models")
    if not isinstance(models_raw, list):
        raise ValueError("API response missing 'models' list")
    slim_models = [normalize_orca_model(m) for m in models_raw if isinstance(m, dict) and m.get("id")]
    return filter_slim_catalog_tool_calling_only(
        {
            "updated_at": raw_api.get("updated_at"),
            "models": slim_models,
            "_meta": {
                "source_url": source_url,
                "generator": generator,
            },
        }
    )


def orca_slim_to_model_capability(slim: dict[str, Any]) -> ModelCapability:
    """Map slim Orca record to ModelCapability bitmask."""
    caps = ModelCapability.NONE
    ins = set(slim.get("input_modalities") or [])
    outs = set(slim.get("output_modalities") or [])
    params = set(slim.get("supported_parameters") or [])

    if "text" in outs:
        caps |= ModelCapability.CHAT
    if "image" in ins:
        caps |= ModelCapability.VISION
    if "audio" in ins or "audio" in outs:
        caps |= ModelCapability.AUDIO
    if "image" in outs:
        caps |= ModelCapability.IMAGE
    if "embeddings" in outs:
        caps |= ModelCapability.EMBEDDINGS
    if "tools" in params:
        caps |= ModelCapability.TOOLS
    return caps


_CAPABILITY_LABEL_ORDER = (
    "CHAT",
    "IMAGE",
    "EMBEDDINGS",
    "AUDIO",
    "MODERATIONS",
    "REALTIME",
    "CODE",
    "VISION",
    "TOOLS",
)


def format_capability_labels(mask: int | ModelCapability) -> str:
    """Human-readable flag names for a capability bitmask (warnings / logs)."""
    v = int(mask)
    if v == 0:
        return "(none)"
    parts: list[str] = []
    for name in _CAPABILITY_LABEL_ORDER:
        m = int(getattr(ModelCapability, name))
        if v & m:
            parts.append(name)
            v &= ~m
    if v:
        parts.append(f"remainder=0x{v:x}")
    return ", ".join(parts)


def context_length_mismatch_warning(
    openrouter_id: str,
    display_name: str,
    existing: Any,
    orca_value: Any,
) -> str | None:
    """Return a warning line if curated context_length does not match Orca (before merge)."""

    def _as_int(x: Any) -> int | None:
        if x is None:
            return None
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    ei = _as_int(existing)
    oi = _as_int(orca_value)
    if ei == oi:
        return None
    return f"openrouter {openrouter_id!r} ({display_name}): context_length differs — default_models={existing!r} Orca={orca_value!r}"


def capability_mismatch_warning(
    openrouter_id: str,
    display_name: str,
    existing_int: int,
    orca_int: int,
) -> str | None:
    """Return a warning line if curated capability bitmask does not match Orca (before merge)."""
    if existing_int == orca_int:
        return None
    only_def = existing_int & ~orca_int
    only_orc = orca_int & ~existing_int
    return (
        f"openrouter {openrouter_id!r} ({display_name}): capability differs — "
        f"default_models=[{format_capability_labels(existing_int)}] "
        f"Orca=[{format_capability_labels(orca_int)}] "
        f"(only in default_models: [{format_capability_labels(only_def)}]; "
        f"only in Orca: [{format_capability_labels(only_orc)}])"
    )


def merge_default_entry_from_slim(
    entry: dict[str, Any],
    slim: dict[str, Any] | None,
) -> dict[str, Any]:
    """Update context_length and OR-in capabilities from a slim Orca row."""
    if slim is None:
        return entry
    api_caps = orca_slim_to_model_capability(slim)
    old = entry.get("capability", ModelCapability.NONE)
    oi = int(old)
    entry["capability"] = ModelCapability(oi | int(api_caps))

    cl = slim.get("context_length")
    if cl is not None:
        try:
            n = int(cl)
        except (TypeError, ValueError):
            pass
        else:
            prev = entry.get("context_length")
            if prev is None:
                entry["context_length"] = n
            else:
                try:
                    entry["context_length"] = max(int(prev), n)
                except (TypeError, ValueError):
                    entry["context_length"] = n
    return entry
