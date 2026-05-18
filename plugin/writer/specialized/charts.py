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
"""

import logging
from plugin.framework.tool import ToolBaseDummy
from plugin.calc.charts import (
    ListCharts as CalcListCharts,
    GetChartInfo as CalcGetChartInfo,
    CreateChart as CalcCreateChart,
    EditChart as CalcEditChart,
    DeleteChart as CalcDeleteChart,
    ManageCharts as CalcManageCharts,
)

log = logging.getLogger("writeragent.writer")

# Union services: Writer wrappers share tool names with Calc; last registration wins,
# so both must be listed or spreadsheets fail ToolRegistry.execute compatibility.
_ALL_CHART_DOCS = [
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.drawing.DrawingDocument",
    "com.sun.star.presentation.PresentationDocument",
]


class ListCharts(CalcListCharts, ToolBaseDummy):  # type: ignore[misc]  # dual base: Calc tool + registry skip marker
    uno_services = _ALL_CHART_DOCS


class GetChartInfo(CalcGetChartInfo, ToolBaseDummy):  # type: ignore[misc]
    uno_services = _ALL_CHART_DOCS


class CreateChart(CalcCreateChart, ToolBaseDummy):  # type: ignore[misc]
    uno_services = _ALL_CHART_DOCS


class EditChart(CalcEditChart, ToolBaseDummy):  # type: ignore[misc]
    uno_services = _ALL_CHART_DOCS


class DeleteChart(CalcDeleteChart, ToolBaseDummy):  # type: ignore[misc]
    uno_services = _ALL_CHART_DOCS


class ManageCharts(CalcManageCharts, ToolBaseDummy):  # type: ignore[misc]
    uno_services = _ALL_CHART_DOCS

