# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Quant helper templates (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.quant`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_registry import names

_QUANT_VENV_EXPORTS = names("quant") | {"run_quant"}

install_lazy_exports(globals(), "quant", _QUANT_VENV_EXPORTS)


# --- Templates ---


def parse_quant_script_header(code: str) -> Any:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("quant").parse_header(code)


def get_quant_script_templates() -> dict[str, str]:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("quant").templates()


def get_quant_template(helper: str) -> str | None:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("quant").template_for(helper)
