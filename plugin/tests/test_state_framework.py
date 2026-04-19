"""Tests for plugin.framework.state FSM contracts."""

import dataclasses

import pytest

from plugin.framework.state import (
    BaseState,
    FsmTransition,
)


@dataclasses.dataclass(frozen=True)
class _TrivialState(BaseState):
    n: int = 0


def _trivial_next(state: _TrivialState, increment: bool) -> FsmTransition[_TrivialState]:
    if increment:
        return FsmTransition(
            state=dataclasses.replace(state, n=state.n + 1),
            effects=["tick"],
        )
    return FsmTransition(state=state, effects=[])


def test_base_state_subclass_frozen():
    s = _TrivialState(n=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.n = 2  # type: ignore[misc]


def test_fsm_transition_immutable():
    t = FsmTransition(_TrivialState(n=0), [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.state = _TrivialState(n=1)  # type: ignore[misc]


def test_trivial_next_pure():
    s0 = _TrivialState()
    t1 = _trivial_next(s0, True)
    assert s0.n == 0
    assert t1.state.n == 1
    assert t1.effects == ["tick"]
    t2 = _trivial_next(t1.state, False)
    assert t2.state.n == 1
    assert t2.effects == []
