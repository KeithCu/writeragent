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
"""Draw charting tools leveraging shared chart implementation."""

import logging
from plugin.modules.draw.base import ToolDrawChartBase
from plugin.modules.calc.charts import ListCharts as CalcListCharts, GetChartInfo as CalcGetChartInfo, CreateChart as CalcCreateChart, EditChart as CalcEditChart, DeleteChart as CalcDeleteChart

log = logging.getLogger("writeragent.draw")

_ALL_CHART_DOCS = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument", "com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument"]


class ListCharts(CalcListCharts, ToolDrawChartBase):
    name = "list_charts"
    uno_services = _ALL_CHART_DOCS


class GetChartInfo(CalcGetChartInfo, ToolDrawChartBase):
    name = "get_chart_info"
    uno_services = _ALL_CHART_DOCS


class CreateChart(CalcCreateChart, ToolDrawChartBase):
    name = "create_chart"
    uno_services = _ALL_CHART_DOCS


class EditChart(CalcEditChart, ToolDrawChartBase):
    name = "edit_chart"
    uno_services = _ALL_CHART_DOCS


class DeleteChart(CalcDeleteChart, ToolDrawChartBase):
    name = "delete_chart"
    uno_services = _ALL_CHART_DOCS
