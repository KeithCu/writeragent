# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared contracts for pure finite-state-machine transitions.

Each domain (tool loop, send button, MCP, etc.) defines:
- A frozen dataclass state type inheriting :class:`BaseState`
- A module-level ``next_state(state, event) -> FsmTransition[StateT]`` that is
  **pure**: no I/O, logging, UNO, or :class:`~plugin.framework.event_bus.EventBus`.

Side effects are described only as **effect** values in ``FsmTransition.effects``;
imperative code (mixins, panels) interprets them on the main thread.

**EventBus** is for loose cross-module notifications (config, menu, MCP). Do not
subscribe or emit from inside ``next_state``. If a transition should trigger a
bus notification, return a dedicated effect and emit from the interpreter.

Prefer new effects as ``@dataclass(frozen=True)`` types. String tokens (e.g.
``\"exit_loop\"``) remain valid where legacy code still uses them; they satisfy
the empty :class:`Effect` protocol structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, List, Protocol, Tuple, TypeVar, Union, cast

__all__ = [
    "BaseState",
    "Effect",
    "FsmTransition",
    "unpack_transition",
]


@dataclass(frozen=True)
class BaseState:
    """Marker base for immutable FSM state. Subclasses add domain fields."""


class Effect(Protocol):
    """Structural marker for side-effect descriptions (interpreted outside FSM)."""


StateT = TypeVar("StateT", bound=BaseState)


@dataclass(frozen=True)
class FsmTransition(Generic[StateT]):
    """Result of a pure transition: successor state and effects to run."""

    state: StateT
    effects: List[Any]


def unpack_transition(
    t: Union[FsmTransition[StateT], Tuple[StateT, List[Any]]],
) -> FsmTransition[StateT]:
    """Normalize legacy ``(state, effects)`` tuples to :class:`FsmTransition`."""
    if isinstance(t, FsmTransition):
        return t
    state, effects = t
    return FsmTransition(state=state, effects=list(effects))
