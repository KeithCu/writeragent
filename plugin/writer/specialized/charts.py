# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer charting tools leveraging Calc's chart implementation.

These operate on the active Writer context but use the same UNO paths as Calc chart
tools (embedded chart / sheet-style APIs), not a dedicated chart2 Writer-only module.

Four chart tools are registered; host adapters live in ``plugin.calc.charts``.
"""

from typing import ClassVar

from ..specialized_base import ToolWriterChartBase
from plugin.calc.charts import ChartDelete as CalcChartDelete
from plugin.calc.charts import ChartGetInfo as CalcChartGetInfo
from plugin.calc.charts import ChartList as CalcChartList
from plugin.calc.charts import ChartUpsert as CalcChartUpsert
from plugin.framework.constants import ALL_DOCUMENT_SERVICES

# Union services: same names as Calc/Draw chart tools; last registration wins, so include
# all chart-capable document services (cf. ``plugin.writer.specialized.shapes``).
# Calc's chart class is the first base, so MRO would find ToolCalcSpecialBase.required_core_tools
# (sheet readers) and hide Writer's document readers. A class-body assignment wins.


class ChartList(CalcChartList, ToolWriterChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = ToolWriterChartBase.required_core_tools


class ChartGetInfo(CalcChartGetInfo, ToolWriterChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = ToolWriterChartBase.required_core_tools


class ChartUpsert(CalcChartUpsert, ToolWriterChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = ToolWriterChartBase.required_core_tools


class ChartDelete(CalcChartDelete, ToolWriterChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = ToolWriterChartBase.required_core_tools
