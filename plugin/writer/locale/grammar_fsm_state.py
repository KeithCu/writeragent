# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""FSM state, events, effects, and transitions for the grammar checker worker."""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from plugin.framework.service import BaseState, FsmTransition

if TYPE_CHECKING:
    from plugin.writer.locale.grammar_work_queue import GrammarWorkItem


class EventKind(Enum):
    START = auto()
    LANG_DETECT_DONE = auto()
    GRAMMAR_CHECK_DONE = auto()
    ERROR = auto()


@dataclasses.dataclass(frozen=True)
class GrammarEvent:
    kind: EventKind
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class GrammarChunkState(BaseState):
    bcp47: str
    original_bcp47: str
    chunk: List[Tuple['GrammarWorkItem', str]]
    detect_lang_enabled: bool
    detect_lang_instruction: str
    status: str = "init"
    is_done: bool = False


# --- Effects ---

@dataclasses.dataclass(frozen=True)
class ExecuteLanguageDetectEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    detect_lang_instruction: str


@dataclasses.dataclass(frozen=True)
class ExecuteGrammarCheckEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    bcp47: str


@dataclasses.dataclass(frozen=True)
class ApplyLanguageChangeEffect:
    doc_id: str
    sentence_text: str
    new_bcp47: str


@dataclasses.dataclass(frozen=True)
class RequeueIndividualItemEffect:
    item: 'GrammarWorkItem'
    text: str
    new_bcp47: str
    original_bcp47: str


@dataclasses.dataclass(frozen=True)
class ProcessGrammarResultsEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    results: List[Any]
    bcp47: str
    original_bcp47: str
    elapsed_ms: int


@dataclasses.dataclass(frozen=True)
class EmitStatusEffect:
    phase: str
    text: str
    result: str
    elapsed_ms: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class LogEffect:
    level: str
    message: str
    args: Tuple[Any, ...] = ()


def next_state(state: GrammarChunkState, event: GrammarEvent) -> FsmTransition[GrammarChunkState]:
    """Pure transition function for grammar worker chunks."""
    effects: List[Any] = []

    if event.kind == EventKind.START:
        if state.detect_lang_enabled:
            effects.append(ExecuteLanguageDetectEffect(
                chunk=state.chunk,
                detect_lang_instruction=state.detect_lang_instruction
            ))
            return FsmTransition(dataclasses.replace(state, status="detecting_language"), effects)
        else:
            effects.append(ExecuteGrammarCheckEffect(
                chunk=state.chunk,
                bcp47=state.bcp47
            ))
            return FsmTransition(dataclasses.replace(state, status="checking_grammar"), effects)

    elif event.kind == EventKind.LANG_DETECT_DONE:
        detected_langs = event.data.get("detected_langs", [])

        matching_chunk: List[Tuple['GrammarWorkItem', str]] = []

        for idx, d_lang in enumerate(detected_langs):
            item, text = state.chunk[idx]
            if d_lang and d_lang != state.bcp47:
                effects.append(LogEffect("info", "[grammar] Language mismatch detected: %s vs %s. Triggering locale change.", (d_lang, state.bcp47)))
                effects.append(ApplyLanguageChangeEffect(item.doc_id, text, d_lang))
                effects.append(RequeueIndividualItemEffect(item, text, d_lang, state.bcp47))
            else:
                matching_chunk.append((item, text))

        if not matching_chunk:
            return FsmTransition(dataclasses.replace(state, is_done=True, status="done"), effects)

        effects.append(ExecuteGrammarCheckEffect(
            chunk=matching_chunk,
            bcp47=state.bcp47
        ))
        return FsmTransition(dataclasses.replace(state, chunk=matching_chunk, status="checking_grammar"), effects)

    elif event.kind == EventKind.GRAMMAR_CHECK_DONE:
        results = event.data.get("results", [])
        elapsed_ms = event.data.get("elapsed_ms", 0)

        if len(results) != len(state.chunk):
            effects.append(LogEffect("warning", "[grammar] LLM batch result count mismatch for chunk: expected %s, got %s. Requeuing items.", (len(state.chunk), len(results))))
            for item, text in state.chunk:
                effects.append(RequeueIndividualItemEffect(item, text, state.bcp47, state.original_bcp47))
        else:
            effects.append(ProcessGrammarResultsEffect(
                chunk=state.chunk,
                results=results,
                bcp47=state.bcp47,
                original_bcp47=state.original_bcp47,
                elapsed_ms=elapsed_ms
            ))

        return FsmTransition(dataclasses.replace(state, is_done=True, status="done"), effects)

    elif event.kind == EventKind.ERROR:
        err = event.data.get("error", "Unknown error")
        effects.append(LogEffect("error", "[grammar] FSM error: %s", (err,)))
        effects.append(EmitStatusEffect("failed", "Batch processing", str(err)))
        return FsmTransition(dataclasses.replace(state, is_done=True, status="error"), effects)

    return FsmTransition(state, effects)
