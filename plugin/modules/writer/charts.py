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
from plugin.modules.writer.base import ToolWriterChartBase
from plugin.modules.calc.charts import ListCharts as CalcListCharts
from plugin.modules.calc.charts import GetChartInfo as CalcGetChartInfo
from plugin.modules.calc.charts import CreateChart as CalcCreateChart
from plugin.modules.calc.charts import EditChart as CalcEditChart
from plugin.modules.calc.charts import DeleteChart as CalcDeleteChart

log = logging.getLogger("writeragent.writer")


class ListCharts(CalcListCharts, ToolWriterChartBase):
    name = "list_charts"
    uno_services = ["com.sun.star.text.TextDocument"]


class GetChartInfo(CalcGetChartInfo, ToolWriterChartBase):
    name = "get_chart_info"
    uno_services = ["com.sun.star.text.TextDocument"]


class CreateChart(CalcCreateChart, ToolWriterChartBase):
    name = "create_chart"
    uno_services = ["com.sun.star.text.TextDocument"]


class EditChart(CalcEditChart, ToolWriterChartBase):
    name = "edit_chart"
    uno_services = ["com.sun.star.text.TextDocument"]


class DeleteChart(CalcDeleteChart, ToolWriterChartBase):
    name = "delete_chart"
    uno_services = ["com.sun.star.text.TextDocument"]
