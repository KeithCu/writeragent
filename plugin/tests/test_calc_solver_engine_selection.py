# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for calc_solver engine selection (no UNO document required)."""

import unittest

from plugin.modules.calc.analysis import (
    _impl_name_is_java_nlp_headless_unsafe,
    _should_reject_solver_for_headless,
    _user_requested_java_nlp_engine,
)


class _FakeSolver:
    def __init__(self, impl_name: str):
        self._impl_name = impl_name

    def getImplementationName(self) -> str:
        return self._impl_name


class TestCalcSolverEngineSelection(unittest.TestCase):
    def test_deps_impl_name_is_unsafe_without_nlpsolver_substring(self) -> None:
        self.assertTrue(
            _impl_name_is_java_nlp_headless_unsafe(
                "com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl"
            )
        )

    def test_nlpsolver_in_name_is_unsafe(self) -> None:
        self.assertTrue(_impl_name_is_java_nlp_headless_unsafe("Some.NLPSolver.Foo"))

    def test_coinmp_not_unsafe(self) -> None:
        self.assertFalse(
            _impl_name_is_java_nlp_headless_unsafe("com.sun.star.comp.Calc.CoinMPSolver")
        )

    def test_user_requested_java_nlp(self) -> None:
        self.assertTrue(_user_requested_java_nlp_engine("com.sun.star.comp.Calc.NLPSolver.X"))
        self.assertFalse(_user_requested_java_nlp_engine("com.sun.star.sheet.SolverLinear"))
        self.assertFalse(_user_requested_java_nlp_engine(None))
        self.assertFalse(_user_requested_java_nlp_engine("com.sun.star.sheet.Solver"))

    def test_reject_deps_when_not_explicitly_requested(self) -> None:
        s = _FakeSolver("com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl")
        self.assertTrue(_should_reject_solver_for_headless(None, s))
        self.assertTrue(_should_reject_solver_for_headless("com.sun.star.sheet.SolverLinear", s))

    def test_allow_deps_when_user_requested_nlpsolver(self) -> None:
        s = _FakeSolver("com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl")
        self.assertFalse(
            _should_reject_solver_for_headless("com.sun.star.comp.Calc.NLPSolver.DEPS", s)
        )


if __name__ == "__main__":
    unittest.main()
