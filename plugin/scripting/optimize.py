# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Optimize helper host RPC and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.optimize`` via ``__getattr__``.
"""

from __future__ import annotations

import functools

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import insert_helper_result_into_calc
from plugin.scripting.helper_registry import names

_OPTIMIZE_VENV_EXPORTS = names("optimize") | {"run_optimize"}

install_lazy_exports(globals(), "optimize", _OPTIMIZE_VENV_EXPORTS)


# --- Egress ---

insert_optimize_result_into_calc = functools.partial(
    insert_helper_result_into_calc,
    domain_label="Optimization",
    default_helper="optimization",
    failed_message="Optimization failed.",
)
