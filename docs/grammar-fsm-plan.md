# Grammar Checker FSM Refactoring Plan

## 1. Problem Statement
The current `run_llm_and_cache_batch` function in `plugin/writer/locale/grammar_work_queue.py` has grown into a monolithic procedural block. It handles filtering, caching, language detection, mismatch recovery (fallback to individual items), LibreOffice property updates, LLM requests (for both detection and grammar), batching, and side-effect emission (UI status and logging). 

This makes it difficult to trace, test, and maintain. By applying the Finite State Machine (FSM) pattern from the chatbot (`ToolLoopState` / `ToolCallingMixin`), we can decouple the **control flow** from the **side effects**.

## 2. Proposed Architecture

### 2.1 State & Event Definitions (`plugin/writer/locale/grammar_fsm_state.py`)
Following `plugin.framework.service.BaseState`, we will define a set of immutable dataclasses for state, events, and effects.

**State:**
```python
@dataclasses.dataclass(frozen=True)
class GrammarBatchState(BaseState):
    doc_id: str
    bcp47: str
    items: List[GrammarWorkItem]
    detect_lang_enabled: bool
    status: str = "init"
    is_done: bool = False
```

**Events (`EventKind`):**
- `START`: Kick off the batch processing.
- `LANG_DETECT_DONE`: Language detection LLM returned results.
- `GRAMMAR_CHECK_DONE`: Grammar LLM returned results.
- `ERROR`: An exception or failure occurred.

### 2.2 Effects (Pure Data Objects)
Side effects are returned by the transition function and executed by the worker engine.

```python
@dataclasses.dataclass(frozen=True)
class ExecuteLanguageDetectEffect:
    items: List[GrammarWorkItem]
    locales_in_use: List[str]

@dataclasses.dataclass(frozen=True)
class ExecuteGrammarCheckEffect:
    items: List[GrammarWorkItem]

@dataclasses.dataclass(frozen=True)
class ApplyLanguageChangeEffect:
    doc_id: str
    sentence_text: str
    new_bcp47: str

@dataclasses.dataclass(frozen=True)
class RequeueIndividualItemEffect:
    item: GrammarWorkItem
    new_bcp47: str
    original_bcp47: str

@dataclasses.dataclass(frozen=True)
class CacheResultsEffect:
    bcp47: str
    original_bcp47: str
    doc_id: str
    text: str
    norms: List[Any]

@dataclasses.dataclass(frozen=True)
class EmitStatusEffect:
    phase: str
    text: str
    result: str
    elapsed_ms: Optional[int] = None
```

### 2.3 Pure Transition Function
A `next_state(state: GrammarBatchState, event: GrammarEvent) -> FsmTransition` function will handle logic purely:

- **On `START`:**
  - If `state.detect_lang_enabled` and there are valid complete sentences, yield `ExecuteLanguageDetectEffect` and transition to `status="detecting_language"`.
  - Else, yield `ExecuteGrammarCheckEffect` and transition to `status="checking_grammar"`.

- **On `LANG_DETECT_DONE`:**
  - Identify matches and mismatches.
  - For mismatches: yield `ApplyLanguageChangeEffect` and `RequeueIndividualItemEffect`.
  - For matches: assemble a chunk and yield `ExecuteGrammarCheckEffect`.
  - Transition to `status="checking_grammar"`.

- **On `GRAMMAR_CHECK_DONE`:**
  - If the LLM returned fewer results than items (mismatch), yield `RequeueIndividualItemEffect` for each item to run individually.
  - If successful, parse and normalize errors, then yield `CacheResultsEffect` and `EmitStatusEffect`.
  - Transition to `is_done=True`.

### 2.4 The FSM Engine (`plugin/writer/locale/grammar_fsm.py` or mixed into `grammar_work_queue.py`)
The grammar worker's queue drain loop currently calls `run_llm_and_cache_batch`. This will be replaced by a loop that initializes the `GrammarBatchState`, emits the `START` event, and evaluates `next_state`. 

Because the grammar worker runs sequentially in its own background thread (unlike the chatbot which spawns new async threads per tool), the engine will likely just execute the effects synchronously:
```python
def process_batch(items: List[GrammarWorkItem]):
    state = GrammarBatchState(items=items, ...)
    tr = next_state(state, Event(START))
    
    while not state.is_done:
        state = tr.state
        for effect in tr.effects:
            if isinstance(effect, ExecuteLanguageDetectEffect):
                # Run sync LLM call
                # Create LANG_DETECT_DONE event
                event = Event(LANG_DETECT_DONE, data=...)
            elif isinstance(effect, ExecuteGrammarCheckEffect):
                # Run sync LLM call
                # Create GRAMMAR_CHECK_DONE event
                event = Event(GRAMMAR_CHECK_DONE, data=...)
            # ... execute other effects (apply UNO property, write cache, emit status)
            
        if state.is_done:
            break
            
        tr = next_state(state, event)
```

## 3. Execution Plan
1. **Create `grammar_fsm_state.py`**: Define `GrammarBatchState`, `EventKind`, `GrammarEvent`, and the Effect dataclasses. Implement the `next_state` function.
2. **Unit Tests**: Write pure unit tests for `next_state` (testing matches, language mismatches, fallback to single item). This will be highly testable since it involves no UNO or LLM dependencies.
3. **Engine Implementation**: Create an `_execute_effect` dispatcher in `grammar_work_queue.py`.
4. **Refactor**: Swap `run_llm_and_cache_batch` over to use the FSM. 
5. **Clean up**: Remove the deeply nested loops and conditionals from the worker thread.
