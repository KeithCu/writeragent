# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures shared by the Calc =PY() unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.calc.python.calc_workbook_test_access import reset_calc_workbook_states


@pytest.fixture(autouse=True)
def _reset_calc_workbook_states_after_test() -> Iterator[None]:
    """Cancel armed debounce timers after each test.

    A spill write left armed runs 0.1 s later on the calc-debounce thread and
    calls main.post, which by then may be another test's stub.
    """
    yield
    reset_calc_workbook_states()
