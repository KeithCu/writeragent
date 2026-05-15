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
"""Guard wiring for ``plugin.tests.calc.test_calc_uno`` menu facade module lists."""

import importlib


def test_calc_menu_facade_exports_and_partition():
    from plugin.tests.calc import test_calc_uno as facade

    assert facade.SKIP_NATIVE_RUN_ALL is True
    assert callable(facade.run_calc_tests)
    assert callable(facade.run_integration_tests)

    unit = set(facade.CALC_MENU_UNIT_MODULES)
    integration = set(facade.CALC_MENU_INTEGRATION_MODULES)
    assert unit.isdisjoint(integration), "unit and integration menu lists must not overlap"

    for dotted in facade.CALC_MENU_UNIT_MODULES + facade.CALC_MENU_INTEGRATION_MODULES:
        importlib.import_module(dotted)
