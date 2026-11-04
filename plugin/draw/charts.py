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
"""Draw charting tools leveraging shared chart implementation.

Four chart tools are registered; host adapters live in ``plugin.calc.charts``.
"""

from typing import ClassVar

from plugin.calc.charts import ChartDelete as CalcChartDelete
from plugin.calc.charts import ChartGetInfo as CalcChartGetInfo
from plugin.calc.charts import ChartList as CalcChartList
from plugin.calc.charts import ChartUpsert as CalcChartUpsert
from plugin.draw.base import ToolDrawChartBase
from plugin.framework.constants import ALL_DOCUMENT_SERVICES


class ChartList(CalcChartList, ToolDrawChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = None


class ChartGetInfo(CalcChartGetInfo, ToolDrawChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = None


class ChartUpsert(CalcChartUpsert, ToolDrawChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = None


class ChartDelete(CalcChartDelete, ToolDrawChartBase):  # type: ignore[misc]
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)  # type: ignore[assignment]
    required_core_tools: ClassVar[frozenset[str] | None] = None
