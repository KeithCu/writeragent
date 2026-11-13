# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for calc_solver engine selection (no UNO document required)."""

from __future__ import annotations

from typing import Any

from plugin.calc.analysis import pick_solver


class _FakeSolver:
    def __init__(self, impl_name: str):
        self._impl_name = impl_name

    def getImplementationName(self) -> str:
        return self._impl_name


class _FakeEnum:
    def __init__(self, items: list[Any]):
        self._items = items
        self._idx = 0

    def hasMoreElements(self) -> bool:
        return self._idx < len(self._items)

    def nextElement(self) -> Any:
        item = self._items[self._idx]
        self._idx += 1
        return item


class _FakeFactory:
    def __init__(self, impl_name: str, result: Any):
        self._impl_name = impl_name
        self._result = result
        self.called = False

    def getImplementationName(self) -> str:
        return self._impl_name

    def createInstanceWithContext(self, _ctx: Any) -> Any:
        self.called = True
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeSmgr:
    def __init__(self, services: dict[str, Any], factories: list[Any] | None = None):
        self._services = services
        self._factories = factories or []

    def createInstanceWithContext(self, svc: str, _ctx: Any) -> Any:
        val = self._services.get(svc)
        if isinstance(val, Exception):
            raise val
        return val

    def createContentEnumeration(self, _svc: str) -> _FakeEnum:
        return _FakeEnum(self._factories)


class TestPickSolver:
    def test_named_engine_wins(self) -> None:
        custom = _FakeSolver("com.example.CustomSolver")
        coinmp = _FakeSolver("com.sun.star.comp.Calc.CoinMPSolver")
        smgr = _FakeSmgr(
            {
                "com.example.CustomSolver": custom,
                "com.sun.star.comp.Calc.CoinMPSolver": coinmp,
            }
        )
        picked = pick_solver(smgr, object(), "com.example.CustomSolver")
        assert picked == (custom, "com.example.CustomSolver")

    def test_missing_named_engine_falls_back_to_coinmp(self) -> None:
        coinmp = _FakeSolver("com.sun.star.comp.Calc.CoinMPSolver")
        smgr = _FakeSmgr(
            {
                "com.example.MissingSolver": None,
                "com.sun.star.comp.Calc.CoinMPSolver": coinmp,
            }
        )
        picked = pick_solver(smgr, object(), "com.example.MissingSolver")
        assert picked == (coinmp, "com.sun.star.comp.Calc.CoinMPSolver")

    def test_enumeration_skips_java_nlp_without_instantiating_and_picks_swarm(self) -> None:
        deps = _FakeFactory("com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl", _FakeSolver("deps"))
        sco = _FakeFactory("com.sun.star.comp.Calc.NLPSolver.SCOSolverImpl", _FakeSolver("sco"))
        swarm_solver = _FakeSolver("com.sun.star.comp.Calc.SwarmSolver")
        swarm = _FakeFactory("com.sun.star.comp.Calc.SwarmSolver", swarm_solver)

        smgr = _FakeSmgr(
            {
                "com.sun.star.sheet.SolverLinear": None,
                "com.sun.star.comp.Calc.CoinMPSolver": RuntimeError("no libsolverlo"),
                "com.sun.star.comp.Calc.LpsolveSolver": None,
            },
            factories=[deps, sco, swarm],
        )
        picked = pick_solver(smgr, object(), "com.sun.star.sheet.Solver")
        assert picked == (swarm_solver, "enumeration:com.sun.star.comp.Calc.SwarmSolver")
        assert not deps.called
        assert not sco.called
        assert swarm.called

    def test_generic_java_nlp_rejected_when_not_requested(self) -> None:
        deps = _FakeSolver("com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl")
        smgr = _FakeSmgr({"com.sun.star.sheet.Solver": deps})
        assert pick_solver(smgr, object(), None) is None

    def test_generic_java_nlp_allowed_when_explicitly_named(self) -> None:
        deps = _FakeSolver("com.sun.star.comp.Calc.NLPSolver.DEPSSolverImpl")
        smgr = _FakeSmgr(
            {
                "com.sun.star.comp.Calc.NLPSolver.DEPS": None,
                "com.sun.star.sheet.Solver": deps,
            }
        )
        picked = pick_solver(smgr, object(), "com.sun.star.comp.Calc.NLPSolver.DEPS")
        assert picked == (deps, "com.sun.star.sheet.Solver")

    def test_enumeration_skips_failing_factory(self) -> None:
        broken = _FakeFactory("com.sun.star.comp.Calc.BrokenSolver", RuntimeError("cannot activate"))
        swarm_solver = _FakeSolver("com.sun.star.comp.Calc.SwarmSolver")
        swarm = _FakeFactory("com.sun.star.comp.Calc.SwarmSolver", swarm_solver)

        smgr = _FakeSmgr({}, factories=[broken, swarm])
        picked = pick_solver(smgr, object(), None)
        assert picked == (swarm_solver, "enumeration:com.sun.star.comp.Calc.SwarmSolver")
