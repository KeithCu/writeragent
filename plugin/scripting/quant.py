# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Quant helper constants (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.quant`` via ``__getattr__``.
"""

from __future__ import annotations

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_registry import names

_QUANT_VENV_EXPORTS = names("quant") | {"run_quant"}

install_lazy_exports(globals(), "quant", _QUANT_VENV_EXPORTS)
