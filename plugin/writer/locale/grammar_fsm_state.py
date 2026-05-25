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


# --- Language Validation ---

@dataclasses.dataclass(frozen=True)
class LanguageValidationState(BaseState):
    chunk: List[Tuple['GrammarWorkItem', str]]
    target_bcp47: str
    instruction: str
    status: str = "init"
    is_done: bool = False
    result_chunk: Optional[List[Tuple['GrammarWorkItem', str]]] = None


@dataclasses.dataclass(frozen=True)
class ExecuteLanguageDetectEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    detect_lang_instruction: str


@dataclasses.dataclass(frozen=True)
class ApplyLanguageChangeEffect:
    doc_id: str
    sentence_text: str
    new_bcp47: str


def next_language_state(state: LanguageValidationState, event: GrammarEvent) -> FsmTransition[LanguageValidationState]:
    """Transitions for the language detection and validation stage."""
    effects: List[Any] = []

    if event.kind == EventKind.START:
        effects.append(ExecuteLanguageDetectEffect(
            chunk=state.chunk,
            detect_lang_instruction=state.instruction
        ))
        return FsmTransition(dataclasses.replace(state, status="detecting_language"), effects)

    elif event.kind == EventKind.LANG_DETECT_DONE:
        detected_langs = event.data.get("detected_langs", [])

        # Optimization: if the entire chunk (often just 1 item) mismatches the same way,
        # we can potentially just update the locale and proceed.
        if len(state.chunk) == 1:
            d_lang = detected_langs[0]
            item, text = state.chunk[0]
            if d_lang and d_lang != state.target_bcp47:
                effects.append(LogEffect("info", "[grammar] Single item language mismatch: %s -> %s. Proceeding with new locale.", (state.target_bcp47, d_lang)))
                # We return the chunk with the NEW target_bcp47 to proceed to grammar stage immediately.
                return FsmTransition(dataclasses.replace(state, is_done=True, status="done", target_bcp47=d_lang, result_chunk=state.chunk), effects)

        matching_chunk: List[Tuple['GrammarWorkItem', str]] = []

        for idx, d_lang in enumerate(detected_langs):
            item, text = state.chunk[idx]
            if d_lang and d_lang != state.target_bcp47:
                effects.append(LogEffect("info", "[grammar] Language mismatch detected: %s vs %s. Triggering locale change.", (d_lang, state.target_bcp47)))
                effects.append(RequeueIndividualItemEffect(item, text, d_lang, state.target_bcp47))
            else:
                matching_chunk.append((item, text))

        return FsmTransition(dataclasses.replace(state, is_done=True, status="done", result_chunk=matching_chunk), effects)
    elif event.kind == EventKind.ERROR:
        err = event.data.get("error", "Unknown error")
        effects.append(LogEffect("error", "[grammar] Language FSM error: %s", (err,)))
        effects.append(EmitStatusEffect("failed", "Language detection", str(err)))
        return FsmTransition(dataclasses.replace(state, is_done=True, status="error"), effects)

    return FsmTransition(state, effects)


# --- Grammar Checking ---

@dataclasses.dataclass(frozen=True)
class GrammarCheckState(BaseState):
    chunk: List[Tuple['GrammarWorkItem', str]]
    bcp47: str
    original_bcp47: str
    status: str = "init"
    is_done: bool = False


@dataclasses.dataclass(frozen=True)
class ExecuteGrammarCheckEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    bcp47: str


@dataclasses.dataclass(frozen=True)
class ProcessGrammarResultsEffect:
    chunk: List[Tuple['GrammarWorkItem', str]]
    results: List[Any]
    bcp47: str
    original_bcp47: str
    elapsed_ms: int


def next_grammar_state(state: GrammarCheckState, event: GrammarEvent) -> FsmTransition[GrammarCheckState]:
    """Transitions for the grammar check stage."""
    effects: List[Any] = []

    if event.kind == EventKind.START:
        effects.append(ExecuteGrammarCheckEffect(
            chunk=state.chunk,
            bcp47=state.bcp47
        ))
        return FsmTransition(dataclasses.replace(state, status="checking_grammar"), effects)

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
            if state.original_bcp47 and state.original_bcp47 != state.bcp47:
                for item, text in state.chunk:
                    effects.append(ApplyLanguageChangeEffect(item.doc_id, text, state.bcp47))


        return FsmTransition(dataclasses.replace(state, is_done=True, status="done"), effects)

    elif event.kind == EventKind.ERROR:
        err = event.data.get("error", "Unknown error")
        effects.append(LogEffect("error", "[grammar] Grammar FSM error: %s", (err,)))
        effects.append(EmitStatusEffect("failed", "Grammar check", str(err)))
        return FsmTransition(dataclasses.replace(state, is_done=True, status="error"), effects)

    return FsmTransition(state, effects)


# --- Infrastructure ---

@dataclasses.dataclass(frozen=True)
class RequeueIndividualItemEffect:
    item: 'GrammarWorkItem'
    text: str
    new_bcp47: str
    original_bcp47: str


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
