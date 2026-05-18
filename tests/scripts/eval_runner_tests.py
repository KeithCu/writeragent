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
import unittest
from unittest.mock import MagicMock
from plugin.tests.eval_runner import EvalRunner

class TestEvalRunner(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.doc = MagicMock()
        self.model_name = "test-model"
        # Optional: Mock get_config/get_api_config if needed

    def test_runner_init(self):
        runner = EvalRunner(self.ctx, self.doc, self.model_name)
        self.assertEqual(runner.passed, 0)
        self.assertEqual(runner.failed, 0)

if __name__ == "__main__":
    unittest.main()
