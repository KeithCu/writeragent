# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.forecast`` via ``__getattr__``.
"""

from __future__ import annotations

import functools
from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import insert_helper_result_into_calc
from plugin.scripting.helper_registry import names

_FORECAST_VENV_EXPORTS = names("forecast") | {"run_forecast"}

install_lazy_exports(globals(), "forecast", _FORECAST_VENV_EXPORTS)


# --- Templates ---


def parse_forecast_script_header(code: str) -> Any:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("forecast").parse_header(code)


def get_forecast_script_templates() -> dict[str, str]:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("forecast").templates()


def get_forecast_template(helper: str) -> str | None:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("forecast").template_for(helper)


insert_forecast_result_into_calc = functools.partial(
    insert_helper_result_into_calc,
    domain_label="Forecast",
    default_helper="forecast",
    failed_message="Forecast failed.",
)
