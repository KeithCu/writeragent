# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Declarative registry for worker harness run_trusted_action dispatch.

These reviewed modules bypass the AST sandbox and run with full venv privileges.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plugin.framework.deal_shim import deal

if TYPE_CHECKING:
    from collections.abc import Callable


def _split_handler(handler: str) -> tuple[str, str]:
    """Return ``(module, attr)`` for a ``module:attr`` handler string.

    ``rsplit(':', 1)`` keeps extra colons in the module, so ``a:b:c`` was
    ``importlib.import_module('a:b')`` and failed as ModuleNotFoundError.
    The contract is exactly one colon and both sides non-empty.
    """
    mod_name, sep, attr_name = handler.partition(":")
    if sep != ":" or not mod_name or not attr_name or ":" in attr_name:
        raise ValueError(
            f"Trusted action handler must be 'module:attr', got {handler!r}"
        )
    return mod_name, attr_name


@dataclass(frozen=True)
class TrustedActionWiring:
    """One trusted-action domain routed inside the venv worker."""

    domain: str
    handler: str
    supports_heartbeat: bool = False

    def dispatch(self, data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
        mod_name, attr_name = _split_handler(self.handler)
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr_name)
        # supports_heartbeat is the contract that the handler accepts
        # heartbeat_fn. Passing it anyway raises TypeError for a handler
        # that does not take that keyword (and does not take **kwargs).
        if self.supports_heartbeat:
            return fn(data, heartbeat_fn=heartbeat_fn)
        return fn(data)


_VENV_DISPATCH = "plugin.scripting.venv.trusted_dispatch:"

_SPEC_DOMAINS: tuple[str, ...] = (
    "units", "symbolic", "viz", "analysis", "forecast", "optimize", "quant",
)

_TRUSTED_ACTION_WIRING: tuple[TrustedActionWiring, ...] = (
    *(TrustedActionWiring(domain, f"{_VENV_DISPATCH}dispatch_spec_domain") for domain in _SPEC_DOMAINS),
    TrustedActionWiring("text", f"{_VENV_DISPATCH}dispatch_text"),
    TrustedActionWiring("vision", f"{_VENV_DISPATCH}dispatch_vision"),
    TrustedActionWiring("sql", f"{_VENV_DISPATCH}dispatch_sql"),
    TrustedActionWiring("languagetool", f"{_VENV_DISPATCH}dispatch_languagetool"),
    TrustedActionWiring("vale", f"{_VENV_DISPATCH}dispatch_vale"),
    TrustedActionWiring("embedding", f"{_VENV_DISPATCH}dispatch_embedding"),
    TrustedActionWiring("langdetect", f"{_VENV_DISPATCH}dispatch_langdetect"),
    TrustedActionWiring(
        "embeddings_index",
        "plugin.embeddings.venv.embeddings_index_dispatch:dispatch_trusted",
        supports_heartbeat=True,
    ),
    TrustedActionWiring(
        "folder_fts",
        "plugin.embeddings.venv.folder_fts_dispatch:dispatch_trusted",
        supports_heartbeat=True,
    ),
)


def _index_by_domain(
    entries: tuple[TrustedActionWiring, ...],
) -> dict[str, TrustedActionWiring]:
    """Index wiring by domain. A repeated domain shadows today; fail instead."""
    by_domain: dict[str, TrustedActionWiring] = {}
    for wiring in entries:
        if wiring.domain in by_domain:
            raise ValueError(f"Duplicate trusted action domain: {wiring.domain!r}")
        by_domain[wiring.domain] = wiring
    return by_domain


_WIRING_BY_DOMAIN: dict[str, TrustedActionWiring] = _index_by_domain(_TRUSTED_ACTION_WIRING)


@deal.post(lambda result: result is None or isinstance(result, TrustedActionWiring))
def get_trusted_action_wiring(domain: str) -> TrustedActionWiring | None:
    """Return wiring for *domain*, or None when unregistered."""
    return _WIRING_BY_DOMAIN.get(str(domain or ""))
