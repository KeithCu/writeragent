# Real-time / AI grammar checking — plan and status

**Status**: Shipped — UNO proofreader + engine + Linguistic `GrammarCheckers` XCU are bundled in default builds (`make manifest` / `make build`).  
**Authors**: WriterAgent Team  
**Audience**: Developers and PMs aligning on **native Writer linguistic grammar** vs optional **sidebar chat** (different surfaces, different jobs).

---

## 0. Tutorial: LibreOffice grammar proofreader API

This section explains the LibreOffice Linguistic2 proofreader API as WriterAgent uses it. The key idea is that Writer does **not** ask a proofreader to scan the whole document. Writer calls a registered `XProofreader` with a text buffer and offset hints, then expects a `ProofreadingResult` containing errors whose positions are relative to that buffer.

### The moving parts

- **Registration**: WriterAgent registers `WriterAgentAiGrammarProofreader` as a `com.sun.star.linguistic2.Proofreader` service in the extension registry. LibreOffice discovers it through `GrammarCheckers` XCU configuration and the component implementation name.
- **Locale support**: LibreOffice asks `hasLocale()` and `getLocales()` to decide whether this checker applies to the current document language. WriterAgent normalizes UNO locales like `en_US` into BCP-47 tags like `en-US` for cache keys and LLM prompts.
- **Proofreading entry point**: LibreOffice calls `doProofreading(...)`. This is the main hot path. It may be called frequently while typing, while opening a document, when visible text changes, or when the grammar dialog asks for results.
- **Result object**: The proofreader returns a `com.sun.star.linguistic2.ProofreadingResult`. It contains the checked text, sentence boundary fields, and a tuple of `SingleProofreadingError` objects. Writer uses those error spans to draw proofreading underlines.

### `doProofreading` parameters

The method signature in WriterAgent is:

```python
def doProofreading(
    self,
    aDocumentIdentifier: str,
    aText: str,
    aLocale: Any,
    nStartOfSentencePosition: int,
    nSuggestedBehindEndOfSentencePosition: int,
    aProperties: Any,
) -> Any:
```

- **`aDocumentIdentifier`**: LibreOffice's identifier for the document/proofreading context. WriterAgent uses it as part of `inflight_key` so queued work for the same document and locale can supersede older queued work.
- **`aText`**: The text buffer LibreOffice wants checked. Treat this as the coordinate system for offsets. It is not necessarily the whole document; in practice it is paragraph-like or sentence-like text supplied by Writer's linguistic pass.
- **`aLocale`**: The UNO locale for the text being checked. WriterAgent maps this to a canonical BCP-47 tag, then uses that tag for cache keys, supported-locale checks, and the LLM prompt language.
- **`nStartOfSentencePosition`**: LibreOffice's start offset for the sentence or sub-span currently being considered inside `aText`. This is an offset into `aText`, not into the whole document.
- **`nSuggestedBehindEndOfSentencePosition`**: LibreOffice's suggested end offset for the current sentence or current probing span. "Behind end" means one-past-the-last character, like Python slice end indexes.
- **`aProperties`**: Optional linguistic properties from LibreOffice. WriterAgent currently does not depend on it for grammar scheduling.

The two integer parameters are hints from Writer's sentence traversal, not a complete scheduling policy by themselves. In particular, `nStartOfSentencePosition != 0` can mean either "Writer is probing an incremental sub-span while typing" or "Writer is asking about a legitimate later sentence in the same `aText` buffer." The proofreader must classify the call by the surrounding text and sentence boundaries, not by that single number alone.

### `ProofreadingResult` fields Writer cares about

WriterAgent creates and fills a `ProofreadingResult` with these important fields:

- **`aDocumentIdentifier` / `aText` / `aLocale`**: Echo the input context so LibreOffice can associate the result with the original request.
- **`nStartOfSentencePosition`**: The start offset of the span this result describes.
- **`nStartOfNextSentencePosition`**: Where LibreOffice should continue its sentence traversal after this result.
- **`nBehindEndOfSentencePosition`**: The one-past-end offset for the text span this result covers.
- **`aErrors`**: A tuple of `SingleProofreadingError` objects. Each error uses offsets in the `aText` coordinate system.
- **`xProofreader`**: The proofreader instance returning the result.

For each `SingleProofreadingError`, WriterAgent fills:

- **`nErrorStart`**: Start offset of the marked text inside `aText`.
- **`nErrorLength`**: Length of the marked text.
- **`nErrorType`**: `TextMarkupType.PROOFREADING`, so LibreOffice draws grammar/proofreading markup.
- **`aRuleIdentifier`**: Stable enough rule id for ignore handling.
- **`aSuggestions`**: Replacement suggestions shown by Writer.
- **`aShortComment` / `aFullComment`**: Human-readable explanation shown in Writer UI.

### How WriterAgent adapts a synchronous API to async LLM work

`doProofreading` is synchronous from LibreOffice's point of view: Writer calls it and expects a `ProofreadingResult` back now. LLM calls are too slow for that foreground path, so WriterAgent uses a cache-first strategy:

1. Normalize the locale and choose the text span to check.
2. Split that span into sentence candidates.
3. Return cached sentence errors immediately when available.
4. For uncached sentences, enqueue background work and return an empty or partial result.
5. On a later LibreOffice proofreading pass, the sentence cache is warm and `doProofreading` returns real errors synchronously.

This is why the API result boundaries matter so much. They tell Writer what span was handled now, while the queue decides what LLM work will become available for a future pass.

### Paragraph handoff vs typing churn

LibreOffice can call the same API in different situations:

- **Paragraph-scale handoff**: `aText` contains multiple stable sentence candidates, often an entire paragraph. `nStartOfSentencePosition` is usually `0` or a real sentence boundary. `nSuggestedBehindEndOfSentencePosition` may only describe LibreOffice's first sentence guess, but `aText` has more sentences after it. WriterAgent should split the handed-in span and enqueue every uncached sentence candidate so the paragraph is fully covered.
- **Incremental typing span**: `aText` may still be paragraph-like, but `nStartOfSentencePosition` and `nSuggestedBehindEndOfSentencePosition` identify the active range Writer is probing while the user edits. WriterAgent should map that active range to the containing sentence and enqueue only that sentence. Newer versions of the same sentence then replace older queued work through `inflight_key` and `enqueue_seq`.

The design target is therefore **sentence-sized work**, not "only the first sentence." A paragraph can produce multiple sentence-sized queue items; active typing should collapse to the one sentence currently changing.

---

## 1. Native grammar vs chat (do not conflate)

| Surface | UX | Role |
|--------|-----|------|
| **Native Writer grammar (Linguistic2)** | Same as other grammar extensions: Writer’s grammar pass, underlines, grammar dialog. Uses `XProofreader` + `Linguistic` / `GrammarCheckers` registry. | **Shipped / experimental** — Python `XProofreader` + Lightproof-style XCU are in the default OXT; users enable LLM work on the **Doc** tab and pick the active proofreader under Writing aids. Earlier native crashes were fixed by accepting extra UNO constructor args (`__init__(self, ctx, *args)`). |
| **Chat with Document (sidebar)** | Multi-turn chat with document context and tools — not a duplicate linguistic pipeline. | Use this when you want **explanations, rewrites, or whole-paragraph help**; it does not replace underlined in-flow proofreading. |

## 1. Native grammar vs chat (do not conflate)

| Surface | UX | Role |
|--------|-----|------|
| **Native Writer grammar (Linguistic2)** | Same as other grammar extensions: Writer’s grammar pass, underlines, grammar dialog. Uses `XProofreader` + `Linguistic` / `GrammarCheckers` registry. | **Shipped / experimental** — Python `XProofreader` + Lightproof-style XCU are in the default OXT; users enable LLM work on the **Doc** tab and pick the active proofreader under Writing aids. Earlier native crashes were fixed by accepting extra UNO constructor args (`__init__(self, ctx, *args)`). |
| **Chat with Document (sidebar)** | Multi-turn chat with document context and tools — not a duplicate linguistic pipeline. | Use this when you want **explanations, rewrites, or whole-paragraph help**; it does not replace underlined in-flow proofreading. |

### Architectural Scope: Sentence vs. Paragraph
The implementation draws heavily from `Lightproof` as a mature, robust foundation, but evolves significantly. Currently, the checker operates on a **sentence-at-a-time** basis for the native `XProofreader` surface. 

**Why Sentence-Scoped?**
- **Cost & Latency**: Sending a full paragraph every time a single character is typed is computationally expensive and introduces unnecessary latency into the foreground UI.
- **Precision**: By focusing on the sentence, the LLM can provide more accurate, localized error reports, reducing the risk of "offset hallucination" where squiggles appear in the wrong place.
- **Cacheability**: Sentence-level caching is highly effective; semantically identical sentences typed in different parts of a document re-trigger hits, whereas paragraph-level caching would be invalidated by almost any minor edit.

### Sentence-sized LLM work (no routine character cap)

Proofreading uses **sentence boundaries** from `split_into_sentences` (LibreOffice `BreakIterator` hybrid). **`doProofreading`** selects whole sentences: on **`nStart == 0`** it considers **all** sentences in `aText`; on incremental calls it considers sentences overlapping LibreOffice’s active range (nonzero `nStart` no longer skips later sentences). Each cache miss enqueues **one** `GrammarWorkItem` per sentence; the worker sends **one sentence per LLM request** (no multi-sentence concatenation). **`GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS`** (8192) applies only as a safety ceiling for pathological run-on text without terminators.

**The "High-Level" Strategy**
While sentence-scoped checking is excellent for localized grammar, it inherently misses global context (e.g., tone consistency or paragraph-level flow). Rather than forcing the grammar checker to handle high-level context (and increasing cost/complexity), we utilize the **Chat Sidebar + `add_comment` tool**. This allows the model to analyze wide context at once and leave copyeditor-style comments. This separation of concerns—**native squiggles for local grammar, sidebar chat for high-level editorial review**—is a deliberate design choice that optimizes performance while maintaining editorial depth. Future work may explore sliding-window paragraph analysis, but only if optimized to avoid the overhead of full-context re-submission on every edit.

---

## 2. What we shipped (native grammar)

### 2.1 Code and packaging

- **UNO component**: [`plugin/writer/ai_grammar_proofreader.py`](../plugin/writer/ai_grammar_proofreader.py) — `WriterAgentAiGrammarProofreader` (`unohelper` + `XProofreader`, locales, service info). Standalone entrypoint: extends `sys.path` like [`plugin/chatbot/panel_factory.py`](../plugin/chatbot/panel_factory.py) so `import plugin.*` works when LO loads the module.
- **Engine (testable)**: [`plugin/writer/grammar_proofread_engine.py`](../plugin/writer/grammar_proofread_engine.py) — JSON parsing (`safe_json_loads`), offset normalization (with first-occurrence bias note), sentence-level LRU cache, ignore-rule set, hybrid sentence splitter, work-queue dedup.
- **Registry**: [`extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu`](../extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu) — fuses `org.extension.writeragent.comp.pyuno.AiGrammarProofreader` under `GrammarCheckers` with `Locales` set to a space-separated list of BCP-47 tags (one `oor:string-list` `<value>`, matching Lightproof). Tags are defined in [`grammar_locale_registry.py`](../plugin/writer/grammar_locale_registry.py) as [`GRAMMAR_REGISTRY_LOCALE_TAGS`](../plugin/writer/grammar_proofread_engine.py) (same coverage as shipped gettext `locales/` plus `en-US` / `en-GB`). Must stay aligned with `getLocales()` (UNO `Locale` per tag) and `GRAMMAR_REGISTRY_LOCALE_TAGS` (unit test enforces parity). Document **regional** `CharLocale` values normalize to the canonical tag per language for cache and the LLM prompt.
- **Bundle**: [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) — `META-INF/manifest.xml` always lists the Python UNO module and `registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu` in default `make manifest` / `make build` output.

### 2.2 Configuration

- **All settings (Doc tab)**: `doc.grammar_proofreader_*` in [`plugin/doc/module.yaml`](../plugin/doc/module.yaml) — enable (default **off**), wait timeout (ms), optional model (empty = same as chat `text_model`), and `doc.grammar_proofreader_pause_during_agent` (default **off**) to pause grammar API requests while sidebar chat/agent work is active. LLM max output tokens (512) and the **pathological** slice ceiling **`GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS`** are **fixed in code** in [`ai_grammar_proofreader.py`](../plugin/writer/ai_grammar_proofreader.py). The Doc tab also inlines Calc’s **Max Rows Display** (`calc.max_rows_display` via `config_inline: doc` in [`plugin/calc/module.yaml`](../plugin/calc/module.yaml)).
- **Diagnostics**: logger name `writeragent.grammar` — `INFO` lines prefixed `[grammar]` for each `doProofreading` call, cache hit/miss, worker skip/supersede, LLM request/result counts, and `WARNING` with stack trace on worker failure. Ensure `init_logging` has run (first grammar call attempts it); see `writeragent_debug.log` under the LO user config directory (see AGENTS.md).

### 2.3 Runtime behavior (summary)

- **`doProofreading`** (async return path): On a **full cache miss**, WriterAgent returns with empty `aErrors` and enqueues a work item. On a **partial cache hit** (some sentences cached, some not), it **returns the cached errors immediately** (better than empty — squiggles appear for already-checked sentences) and enqueues for the remaining uncached sentences. On a **full cache hit** all errors are returned directly, no enqueue needed. It **does not** wait inside `doProofreading` or pump `processEventsToIdle()` for results. That keeps **menus and chrome responsive** while grammar runs.
- **`doc.grammar_proofreader_wait_timeout_ms`**: No longer used by the proofreader return path (reserved for possible future options or removed from UI in a later cleanup).
- **Sidebar status**: the proofreader emits `grammar:status` for meaningful phases (`start`, `request`, `complete`, `failed`, etc.). Skipped work is not reported to the status bar.
- **Concurrency / work queue**: A single persistent daemon thread (`_GrammarWorkQueue`) drains a `queue.Queue` sequentially. On each iteration, the worker **batch-drains** all pending items and runs **`deduplicate_grammar_batch`** ([`grammar_proofread_engine.py`](../plugin/writer/grammar_proofread_engine.py)) before processing survivors. This eliminates the prior stampede where N cache misses spawned N workers that all contended for `llm_request_lane` simultaneously.
- **Enqueue-time replace-in-place (O(1) tail check)**: `_GrammarWorkQueue.enqueue()` acquires `queue.Queue`'s own internal mutex (`self._q.mutex`) and checks the **last item** of the internal deque (`self._q.queue`). If it has the same `inflight_key` and the incoming item is newer (higher `enqueue_seq`), it **replaces it in place**. This efficiently collapses typing bursts into a single pending request without a loop. If no match is found at the tail, the item is appended normally.
- **Newest-wins dedup + stale suppression**: The drain-time **`deduplicate_grammar_batch`** pass remains as a belt-and-suspenders safety net: it handles items that slipped past the tail scan (e.g. a deep queue during a burst) and cross-key prefix dedup within each `(doc_id, locale)` group. Within each group, **prefix-related conflicts keep the newest request** (`enqueue_seq`). Items with the same `inflight_key` are also superseded by sequence number (highest survives). **`inflight_key`** is **`{aDocumentIdentifier}|{locale}|{sentence_start}`** (sentence start offset in `aText`; no full sentence text): edits within one sentence supersede queued work for that sentence while sibling sentences in the same paragraph keep distinct keys. `_GrammarWorkQueue` additionally performs a **pre-execute stale check** against `_latest_seq` and skips any survivor older than the latest known sequence for that key, which closes cross-batch race windows. This is combined with a 1-second pause mechanism: the worker collects requests and waits until there is a 1-second period of no new requests before processing the batch, ensuring checks only run when the user stops typing.
- **Queue diagnostics**: Explicit queue logs for enqueue, drain batch size, dedup survivors, stale-skip, and execute; each includes `doc_id`, `inflight_key`, `enqueue_seq`, slice length, and a compact text preview to diagnose intermittent ordering issues. Out-of-order sequence detection in `enqueue` logs at ERROR level if an incoming item has a lower sequence than the latest recorded for that key.
- **Sentence-level gating**: grammar checks run when the slice looks like a complete sentence (terminal punctuation heuristic with multilingual marks such as `. ! ? … ؟ 。 ！ ？ ।`) **or** when partial text reaches `GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS` (15 non-space chars). Short incomplete fragments are skipped before cache/worker scheduling.
- **Sentence cache**: Cache is keyed by **individual sentence** text (locale + fingerprint, trailing whitespace stripped via `rstrip()`). `MAX_CACHE_SIZE` is **2048**. `split_into_sentences` ([`grammar_proofread_engine.py`](../plugin/writer/grammar_proofread_engine.py)) uses a **hybrid approach**: for Western/CJK languages, it delegates to LibreOffice's native `BreakIterator` (via the `_get_break_iterator_and_locale` helper) with a custom heuristic to merge breaks after short abbreviations like "Mr.", "Dr.", "vs." — capped at 3-char words to avoid false positives on proper nouns like "USA.", "Tom."); for Thai, Lao, and Khmer, it intelligently splits on whitespace. On **lookup** (`doProofreading`): each sentence is checked independently — if **all** are cached, combined errors are returned immediately (no enqueue); on **partial hit**, cached errors are returned immediately (squiggles for checked sentences) while uncached sentences are enqueued **one queue item per sentence**. On **worker execution** (`_run_llm_and_cache`): each item is normally one sentence; the worker sends **one sentence per LLM request** and caches that sentence’s errors (pathological multi-fragment slices may trigger multiple sequential requests in one worker invocation).

  New (2026-05): For **incomplete** sentences (no terminator), `cache_put_sentence` performs a cheap newest-first scan (max 10 recent entries) to evict any incomplete strict-prefix predecessors for the same locale. This prevents LRU churn when a user types a long sentence incrementally ("The qu" → "The quick brown fox..."). Complete sentences are protected and never evicted. The common case collapses to 1 LRU slot. See `test_sentence_cache_incomplete_prefix_compaction`.
- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/framework/client/llm_client.py) with `response_format={"type":"json_object"}` on the OpenAI-compatible path (Together, OpenRouter, etc.; see docstring on `make_chat_request`), a system prompt (template moved to top of `ai_grammar_proofreader.py`) requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}` (schema description in English) plus the **document language** (BCP-47 and English name from the registry), and user message the **checked sentence text** for that worker item (one sentence per request in normal prose). The prompt explicitly asks for errors in the order they appear. For threshold-allowed partial slices, the prompt adds a conservative note that input may be partial. Parser: [`parse_grammar_json`](../plugin/writer/grammar_proofread_engine.py) uses `safe_json_loads` then `json_repair` (with logging) when needed.
- **Offset Normalization**: `normalize_errors_for_text` uses **`search_pos` tracking** to handle multiple occurrences of the same erroneous text within a window. It searches for `wrong` substrings starting from the last matched position, matching the LLM's ordered reporting. Global `full_text.find` fallback removed to ensure errors stay within their intended paragraph/slice.
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### 2.3.1 Why `enqueue_seq` exists (queue FIFO is not enough)

**Terminology.** The shipped code uses a **global integer counter** (`_ENQUEUE_SEQ` in [`ai_grammar_proofreader.py`](../plugin/writer/ai_grammar_proofreader.py)), incremented when a cache miss enqueues work; each [`GrammarWorkItem`](../plugin/writer/grammar_proofread_engine.py) stores it as **`enqueue_seq`**. This is **not** the same as `time.monotonic()` — that clock is used elsewhere only for **elapsed milliseconds** on LLM requests (status/diagnostics), not for ordering queue items.

**Why not rely only on “everything goes through `queue.Queue`”?** A FIFO queue orders **`get()` dequeue order** among objects that are actually retrieved in sequence. The grammar worker deliberately does **more** than strict FIFO:

1. **Tail replace-in-place** — For the same `inflight_key`, a newer item can **overwrite** the last slot of the internal deque without establishing a simple FIFO relationship to items already consumed in an **earlier** batch. Queue position alone does not record “this snapshot superseded that one” across batches.

2. **Batch drain + `deduplicate_grammar_batch`** — The worker collects multiple `get()` results into one batch, then resolves conflicts (same key and prefix-related slices). The implementation needs an explicit **“newest wins”** tie-break; it uses **highest `enqueue_seq`**, not only insertion index in the batch.

3. **`_latest_seq` / pre-execute stale skip** — Before calling the LLM, the worker asks whether a **newer** enqueue has already been recorded for that `inflight_key`. That can be true even when the physical queue does not place “newest next” (e.g. tail was replaced, or newer work will appear after the current drain). `_latest_seq[key]` holds the **last assigned sequence** for that key; each item carries its stamp so survivors can be compared to that mirror.

So **`enqueue_seq` is a generation stamp for supersede/dedup semantics**, not a substitute for the queue. Something must play that role whenever work is merged, replaced, or skipped outside pure FIFO.

**Alternatives (same role, different representation):**

| Approach | Notes |
|----------|--------|
| **Per-`inflight_key` counter** | Bump only when enqueueing for that document+locale key. Same semantics as today’s global counter for same-key comparisons; avoids mixing sequence space across unrelated documents (clearer for logs and reasoning). |
| **Enqueue-time monotonic value** | e.g. `time.monotonic()` at enqueue as the order key. Requires discipline if two enqueues share an identical timestamp resolution; still needs to be stored on each `GrammarWorkItem` and mirrored (like `_latest_seq`) for stale checks. |
| **Post-LLM staleness guard** | Keep a generation stamp **and** re-check before `cache_put_sentence` that no newer enqueue superseded this item while the HTTP call was in flight. The current pre-execute `_is_stale` does not cover the whole LLM duration; mitigations today include sentence-text–keyed cache (reduces wrong writes when text changes). |
| **Remove unused plumbing** | `_run_llm_and_cache` accepts `enqueue_seq` but does not use it inside the function body as shipped; a future change could either drop the parameter or use it for a post-LLM guard above. |

### 2.4 Tests

- Unit: [`plugin/tests/test_grammar_proofread_engine.py`](../plugin/tests/test_grammar_proofread_engine.py) — JSON parsing, offset normalization, sentence cache roundtrip, trailing whitespace cache normalization, ignore rules, overlap expansion.
- Unit (work queue dedup): [`plugin/tests/test_grammar_work_queue.py`](../plugin/tests/test_grammar_work_queue.py) — newest-wins prefix conflict resolution, supersede behavior, reverse-prefix chain reproducer (`"What is going on"` → `"W"`), mid-sentence non-prefix edits with shared `inflight_key`, mixed dedup, cross-locale independence.
- Unit (queue stale checks): [`plugin/tests/test_ai_grammar_proofreader_worker.py`](../plugin/tests/test_ai_grammar_proofreader_worker.py) — `_GrammarWorkQueue` stale detection behavior (`_is_stale`) for older-vs-latest sequence handling.
- UNO (native runner): [`plugin/tests/uno/test_ai_grammar_proofreader.py`](../plugin/tests/uno/test_ai_grammar_proofreader.py) — cache path and `ignoreRule` filtering.
- UNO (native runner): [`plugin/tests/uno/test_writer_sentence_splitter.py`](../plugin/tests/uno/test_writer_sentence_splitter.py) — tests the hybrid LO `BreakIterator` and Thai whitespace splitting heuristics.

### 2.5 Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on the **Doc** tab; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | `doProofreading` does **not** wait on the main thread for LLM results (avoids dead menus while grammar runs). HTTP/LLM runs on a background worker; underlines update on a **later** proofreading pass when the sentence cache is ready. |
| Stale underlines | Sentence cache (locale + sentence text fingerprint) plus sequential work queue with newest-wins dedup and pre-execute stale suppression coalesce calls. **Cache hit** → immediate errors; **miss** → empty return once, queue worker fills cache for the next pass. See §6 for evolving this. |
| Concurrent chat agent | Optional guard (`doc.grammar_proofreader_pause_during_agent`) can skip grammar worker calls while chat/agent sends are active; grammar and chat/agent LLM requests also share one in-process request lane to avoid overlap races. |

---

## 3. Lightproof-inspired optimizations

As of **2026-05**, the native grammar checker pairs **sentence-bound work units** with **sentence-level caching** (Lightproof-inspired scheduling ideas, evolved):

1.  **Sentence-sized scheduling**: `doProofreading` maps LibreOffice’s call to **whole sentences** in `aText` (paragraph pass vs incremental overlap). `ProofreadingResult` traversal positions follow the **union of checked sentences** via `_apply_proofreading_end_positions` — no fixed 500-character proofread window.
2.  **Sentence-level caching**: The old slice-level cache (`_proofread_cache` / `make_cache_key` / `cache_get` / `cache_put` keyed by doc + locale + fingerprint + bounds) has been **removed**. All caching now goes through the **sentence-level cache** (`cache_get_sentence` / `cache_put_sentence` in [`grammar_proofread_engine.py`](../plugin/writer/grammar_proofread_engine.py)). Normalization uses `_normalize_for_sentence_cache` so that trailing whitespace is stripped **and** any punctuation after the *first* sentence terminator is ignored for the cache key (`"Hello."` and `"Hello..."` share a key; `"Hello?"` and `"Hello?..."` share one; but `"Hello?"` vs `"Hello."` remain distinct). Errors are clipped to the canonical length. This means semantically equivalent sentence text anywhere in the document reuses the same errors regardless of document position or trailing punctuation style. See §2.3 "Sentence cache" for the full lookup/storage behavior.

---

## 4. Optional reference: `GrammarChecker.py`

The standalone [`GrammarChecker.py`](../GrammarChecker.py) (root of repo) was used historically as a prompt/threading reference. It is **not** bundled as WriterAgent product code. The shipped proofreader does **not** call it.

---

## 5. Future work (suggested backlog)

### Native grammar — hardening and product

1.  **Native Linguistic Integration**: Research using the built-in `SpellChecker` with specialized XML queries (e.g., `<query type='analyze'>`) to perform morphological analysis (stems, part-of-speech) locally before or during LLM work (inspired by `lightproof` morphology caching).
2.  **429 / backoff**: exponential backoff and cooldown in the grammar worker; optionally skip scheduling when sidebar chat is mid-request (shared policy flag).
3.  **Locales**: shipped: `GRAMMAR_REGISTRY_LOCALE_TAGS` matches gettext `locales/` + `en-US` / `en-GB`; optional future regional tags in the XCU if a given LO build requires explicit `hasLocale`/`getLocales` pairing beyond normalization.
4.  **Refresh UX**: LO only shows new squiggles on **subsequent** proofreading passes; document for users; optional future hook if LO exposes a safe “invalidate proofreading” API worth researching.
5.  **Optional model / temperature**: surface more controls in Settings if needed (currently optional grammar model + shared endpoint).
6.  **LRU cache tuning**: **Shipped (baseline):** sentence-level LRU (**2048** entries, `MAX_CACHE_SIZE`). Includes newest-first bounded scan (max 10 recent entries) for incomplete-sentence prefix compaction to avoid churn on long typing sessions. The old slice-level cache has been removed. See `grammar_proofread_engine.py:cache_put_sentence` and new tests. Further ideas: per-document invalidation, TTL, or stats in UI.
7.  **Document-generation invalidation**: If LO exposes a revision counter, generation id, or “document modified” tick, fold it into the cache key or force miss when the full buffer changes even if a slice string matches (reduces risk of stale absolute offsets after edits above the span).
8.  **Shared policy with chat**: Baseline shipped: optional pause-during-agent setting + shared in-process LLM request lane. Future expansion: endpoint-aware policy (per provider/model), richer status UX, and adaptive queue/backoff.
9.  **Smaller / faster grammar model**: Route grammar-only traffic to a cheaper or local model by default; keep “same as chat” as an override (already partially supported via `grammar_proofreader_model`).
10. **Prompt and schema hardening**: Few-shot examples for edge cases (quotes, lists, track changes); strict JSON recovery; optional `response_format` where the API supports it.
11. **Paragraph / traversal tuning**: If Writer scheduling or underlines misbehave on some LO versions, compare our sentence selection vs stock Lightproof’s `len(rText)` and adjust `_apply_proofreading_end_positions` / overlap rules only.
12. **Ignore rules & parity**: Persist `ignoreRule` across sessions; locale-specific ignores if the API evolves.
13. **Observability**: Debug metrics (cache hit rate, worker supersede count, p50/p95 latency from schedule → `cache_put`) behind a verbose flag for field debugging.
14. **Accessibility / UX copy**: Clear user-facing text that grammar is **asynchronous** (squiggles after pause); link to Writing aids selection when multiple proofreaders exist.
15. **LanguageTool-class local checking (research)**: Phased roadmap for a **Python-first** checker consuming LT-open rule data toward LanguageTool-grade quality over time, **without JVM** in-stack (`nlprule`/fork accelerator optional). See [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md).

### Docs / agents

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).
- Optional non-LLM checker roadmap grounded in LT behavior: [`languagetool-local-parity-phased-plan.md`](languagetool-local-parity-phased-plan.md).

---

## 6. Revision history (high level)

- **Earlier draft**: Described a separate sidebar polling + chat-append path; that direction was **dropped** in favor of native linguistic grammar plus existing **Chat with Document** for conversational help.
- **2026-04 (Late)**: Paragraph-level batching was attempted then reverted; cache uses **slice fingerprints** (Lightproof-adjacent ideas, see §3).
- **2026-04-26**: Lightproof-style **capped** batching re-enabled on `nStart==0` (`min(len(aText), 500)` + `_finalize_proofreading_sentence_positions`).
- **2026-04 (Mid)**: Native linguistic grammar **shipped** (Lightproof-style + LLM + cache); this document updated to match reality and list follow-ups.
- **2026-04 debugging**: Locale list fixed to Lightproof-style space-separated BCP-47 list; lazy imports; stub added.
- **2026-04-26**: Registry expanded to all shipped UI translation locales; `grammar_locale_registry` + normalized `CharLocale` for cache/LLM.
- **2026-04 resolution**: LibreOffice calls `createInstanceWithArgumentsAndContext` with extra args; proofreaders must implement `__init__(self, ctx, *args)`.
- **2026-04 (doc)**: Expanded backlog (cache evolution, cost control, LO integration); clarified §2.5 / §3 cache behavior vs. one-slot-per-doc limitation.
- **2026-04-27**: Replaced per-sentence `run_in_background` + debounce-sleep pattern with single sequential `_GrammarWorkQueue` (`queue.Queue` + one daemon thread). Added `deduplicate_grammar_batch` with prefix dedup + sequence-based supersede. Removed `GRAMMAR_WORKER_DEBOUNCE_MS`, `_INFLIGHT_JOBS`, `_wait_for_inflight_job`. `GrammarWorkItem` and dedup live in `grammar_proofread_engine.py` for UNO-free unit testing.
- **2026-04-27 (cache)**: Fixed critical cache bug — the "sentence cache" was caching whole-paragraph batches as one unit (any typing invalidated the key). Now uses true **per-sentence caching**: `split_into_sentences` splits on `[.!?…؟。！？।]+\s+`, lookup/storage operate per sentence. Added **trailing whitespace normalization** (`rstrip()`) so "Hello." and "Hello. " share the same cache key. **Partial cache hits** return cached errors immediately (better than empty). Worker filters to **only uncached sentences** before LLM call, skips entirely if all cached. Errors are attributed to individual sentences by position and cached independently.
- **2026-04-27 (pause)**: Modified `_GrammarWorkQueue._drain_loop` to wait for a 1-second pause in incoming requests before deduplicating and processing the batch, preventing worker stampedes and early LLM requests while the user is actively typing.
- **2026-04-28**: Implemented hybrid sentence splitting in `grammar_proofread_engine.py`. Switched from a pure-Python regex to LibreOffice's `BreakIterator` (for proper quote/ellipsis handling) augmented with a custom abbreviation heuristic, while retaining whitespace chunking specifically for `th`/`lo`/`km`. Moved sentence splitter tests to the UNO native test suite.
- **2026-04-28 (queue race fix)**: Changed grammar queue dedup semantics from longest-prefix-wins to **newest-wins** by `enqueue_seq` for prefix-related conflicts; kept same-key supersede by highest sequence. Added pre-execute stale suppression in `_GrammarWorkQueue` using `_latest_seq` so cross-batch stale survivors are skipped. Added queue diagnostics (enqueue/drain/survivors/stale-skip/execute) and regression tests in `test_grammar_work_queue.py` + stale checks in `test_ai_grammar_proofreader_worker.py`.
- **2026-04-28 (code review cleanup)**: Removed dead slice-level cache (`_proofread_cache`, `make_cache_key`, `cache_get`, `cache_put`) — all caching now sentence-level only. Fixed `_TRAILING_CLOSERS`: removed opening bracket `〖` (U+3016) and duplicate `〛`. Tightened abbreviation heuristic from `len(word) <= 5` to `<= 3` to avoid false-positive sentence merging on proper nouns ("USA.", "Tom."). Added first-occurrence bias comment in `normalize_errors_for_text`. Added comment + assertion + debug log for near-dead Lightproof fallback condition in `_finalize_proofreading_sentence_positions`. Added out-of-order sequence detection logging in `_GrammarWorkQueue.enqueue`. Replaced dead slice-cache tests with sentence-cache roundtrip and whitespace normalization tests.
- **2026-04-28 (hardening)**: Increased `MAX_CACHE_SIZE` to 512. Consolidated `BreakIterator` and `Locale` initialization into `_get_break_iterator_and_locale` helper. Improved `normalize_errors_for_text` reliability with `search_pos` tracking and removed global text search fallback. Moved `GRAMMAR_SYSTEM_PROMPT_TEMPLATE` to top of `ai_grammar_proofreader.py` and updated prompt to explicitly request ordered errors. Added logging for `json_repair`.
- **2026-04-29 (json robustness)**: Fixed a silent JSON corruption bug in `safe_json_loads` (`json_utils.py`) where unescaped LaTeX commands starting with valid JSON escape characters (e.g., `\times` natively parsed as `<tab>imes`, `\nabla` as `<newline>abla`) were being silently evaluated by Python's standard `json.loads` within the streaming client (`plugin/framework/client/llm_client.py`) before reaching our repair logic. Added `_SILENT_CORRUPTIONS` and updated `_repair_latex_clashes` to explicitly replace these literal control characters and word fragments with their properly double-escaped LaTeX equivalents. Additionally, implemented a structural fix by moving literal `\\n`/`\\t` expansion out of `content.py` and down into the specific `html` segment processing in `format_support.py`; this ensures `tex` math blocks bypass expansion entirely and preserve their backslashes. Added regression tests in `plugin/tests/test_writer_math_preservation.py`.
- **2026-05-02 (doc)**: Removed the deferred “sidebar living assistant” track; documented **Chat with Document** as the conversational alternative; fixed enable-location wording (Doc tab); renumbered sections.
- **2026-05-02 (queue)**: **`inflight_key`** no longer includes a fingerprint of the proofread slice. Same-key supersede and `_latest_seq` stale suppression now apply when the user types in the middle of a sentence (successive slices are not prefix-related). Regression: `test_mid_sentence_typing_dedup` in `test_grammar_work_queue.py`.
- **2026-05-04 (queue)**: Added **enqueue-time replace-in-place** to `_GrammarWorkQueue.enqueue()`. Uses CPython `queue.Queue` internals (`self._q.mutex`, `self._q.queue`) for an **O(1) tail check** to replace an existing same-`inflight_key` item with the incoming newer one — no new locks introduced. Drain-time `deduplicate_grammar_batch` is retained as a safety net for cross-key prefix dedup.
- **2026-05-07 (doc)**: Linked [LanguageTool-class phased plan](languagetool-local-parity-phased-plan.md) from §5 and `AGENTS.md`. Plan centers a **Python-first** checker consuming LT-open resources; JVM/Java not part of that roadmap (see §5 item 15).
- **2026-05-07 (doc)**: §1 — **Sentence-scoped native grammar** vs **`add_comment`** / chat for document-wide copyediting; clarifies LLM slice limits and why multi-sentence error dumps are avoided.
- **2026-05-07 (doc)**: §1 — Brief **LanguageTool** contrast (internal sentence segmentation; rules mostly per-sentence; some cross-sentence via special rules)—vs WriterAgent capped LLM context.
- **2026-05-09**: **Sentence-sized proofread work** shipped: removed 500-character batching and `_finalize_proofreading_sentence_positions`; `inflight_key` includes sentence-start offset; one LLM request per sentence; incremental `nStart != 0` paths use overlap classification. See §7.

---

## 7. Dev plan: sentence-sized proofread work

### Goal

Replace the current 500-character proofread batch with sentence-boundary work units. If LibreOffice hands us a whole paragraph, we still cover the whole paragraph by splitting it into sentences and queueing the uncached sentence work. In the common typing path, where Writer repeatedly calls into the checker while the user edits inside one paragraph, the active work collapses to the current sentence rather than resubmitting a 500-character slice.

We are doing this because if we kept sending arbitrary 500-character windows then a sentence could be truncated mid-way, the LLM could hallucinate offsets outside the real sentence, and we would need fragile post-processing such as `_finalize_proofreading_sentence_positions`. By making the work unit exactly one sentence (or a small set of complete sentences on paragraph handoff) we eliminate truncation, simplify attribution, and remove the need for the Lightproof-era finalize hack.

### Why this fits the current architecture

- The cache is already keyed by individual sentence text (`cache_get_sentence` / `cache_put_sentence` in `grammar_proofread_engine.py`).
- The worker already stores errors per sentence after LLM normalization.
- The LLM prompt and `search_pos` offset normalization are most reliable when the checked text is one local unit.
- The existing 1-second quiet-period drain, `inflight_key` supersede, and newest-wins dedup already prevent re-processing entire paragraphs on every character + pause; cached sibling sentences return immediately from `doProofreading` with no enqueue and no LLM call. We are keeping that machinery unchanged because if we touched the queue or cache we would risk re-introducing the stampede and duplicate-work problems that the current design already solves.

### Implementation steps

1. Add helpers in `ai_grammar_proofreader.py` that split the incoming proofread text into sentence-sized work units.
   - Use LibreOffice's `BreakIterator`-backed `split_into_sentences(self.ctx, loc_key, text)` where possible.
   - Return one or more `(n_start, n_end, complete_sentence)` candidates, preserving offsets into `aText`.
   - Keep a hard fallback maximum only for pathological no-boundary text, not as the normal batching unit.
   - We are dropping `GRAMMAR_PROOFREAD_MAX_CHARS` (500) as a first-class batch size because if we retained it then the normal path would still be limited by an arbitrary character count rather than sentence boundaries, re-introducing truncation risk and forcing us to keep the Lightproof finalize logic we are trying to remove. Only a high safety ceiling for text with no terminators remains.

2. Rework `doProofreading` scheduling around paragraph-load vs typing-update behavior.
   - If Writer hands us a paragraph-scale span, enqueue all uncached sentence candidates from that span so the paragraph is fully covered.
   - If Writer hands us an incremental typing span inside an existing paragraph, narrow the work to the sentence containing the edit and let same-key queue supersede drop older versions.
   - We are replacing the blanket `if nStartOfSentencePosition != 0: return` guard with a classification helper that uses `split_into_sentences` to map the provided range to the containing sentence(s) because if we kept the old Lightproof skip then any legitimate later sentence inside a paragraph that arrived with a nonzero start would be ignored forever, leaving parts of the document unproofread.
   - We keep skipping true incremental sub-spans that do not meet the partial-sentence threshold because if we enqueued every sub-span then every keystroke would generate an LLM call even when the sentence is still too short to be meaningful.
   - Set `nStartOfNextSentencePosition` and `nBehindEndOfSentencePosition` to the end of the covered span (now a sentence boundary) so Writer can advance cleanly. We are doing this because if the result positions did not match the actual sentence we checked then Writer's sentence traversal would get out of sync and we would either miss sentences or re-check them unnecessarily.

3. Simplify cache lookup and enqueueing around sentence candidates.
   - Look up each selected sentence in the sentence cache.
   - On hit, return cached errors shifted by that sentence's `n_start`.
   - On miss, enqueue sentence-sized work items. The worker can keep the existing per-sentence filtering, but paragraph handoff should be represented as multiple sentence-sized misses, not one multi-sentence prompt.
   - We are doing the narrowing at `doProofreading` time (instead of leaving it to the worker) because if the worker still had to decide the focus sentence then the return path from `doProofreading` would remain complex and the partial-hit logic would be harder to reason about. By the time we reach the worker the item is already the right size.

4. Preserve the async and queue guarantees.
   - `doProofreading` must still never wait for the LLM or pump the UI loop.
   - Keep `inflight_key = "{aDocumentIdentifier}|{loc_key}"` so mid-sentence edits still supersede older work.
   - We are keeping the single `inflight_key` per (document, locale) even when a paragraph handoff creates multiple sentence-sized items because if we included sentence text in the key then every keystroke inside a sentence would produce a unique key, defeating the supersede and tail-replace logic and causing duplicate LLM calls.
   - Keep newest-wins queue dedup and the one-second pause.

5. Update tests.
   - Add unit coverage for the new sentence-work helper: first sentence, later sentence with nonzero start, paragraph split into multiple sentence candidates, incomplete short skip, incomplete long partial, long no-punctuation fallback.
   - Update UNO proofreader tests that currently use `min(len(text), 500)`.
   - Add a regression proving two cached sentences in one paragraph are returned through sentence-sized candidates rather than one 500-character batch.
   - Add a regression proving that a 1-second pause while editing inside sentence 3 of a 4-sentence paragraph produces an LLM request containing only sentence 3 and returns `ProofreadingResult` boundaries that exactly match sentence 3. We are adding this test because if the narrowing logic were off by one sentence then either adjacent sentences would be re-sent unnecessarily or the edited sentence would be missed.

### Success criteria

- No LLM request contains more than one sentence in normal punctuated prose.
- A paragraph with multiple sentences still gets all sentences checked; paragraph-scale handoff queues multiple sentence-sized work items rather than checking only the first sentence.
- Cached sentence errors still return immediately without worker scheduling.
- Partial/incomplete sentence behavior remains conservative and does not churn the cache while typing.
- The `doProofreading` hot path is short and obvious: split → map active range to focus sentence(s) → per-sentence cache lookup (immediate return on hit) → enqueue only misses → set result positions to the exact chosen sentence end. We are targeting this simplicity because if the hot path remained entangled with 500-character caps, Lightproof finalize, and blanket skips then future maintainers would have to re-learn the same legacy complexity we are removing.

---

## 8. Noticed potential bugs in the WIP branch (`WIP-pure-sentence-caching`)

During review of the `WIP-pure-sentence-caching` branch, several potential issues and architectural risks were identified that should be addressed before merging or as part of the next iteration:

### 1. Inconsistent Sentence Splitting (Synchronization Risk)
The UI thread and the worker thread both call `split_into_sentences`. 
- **The Issue**: If the `ctx` object in the worker provides slightly different locale handling or if the `BreakIterator` state differs from the UI thread, they may disagree on where a sentence ends (e.g., UI thread sees end at index 50, worker sees index 55).
- **Impact**: The worker will cache the result for the 55-char fingerprint, while the UI thread will continue to miss the cache because it is looking for the 50-char fingerprint. This leads to redundant LLM calls and squiggles that never appear.
- **Recommendation**: Ensure the worker uses a consistent `ctx` or, ideally, pass the pre-split text fragments directly in the `GrammarWorkItem` to avoid re-splitting.

### 2. Conservative Abbreviation Heuristic
The current heuristic for merging sentences after abbreviations is limited to words of 3 characters or less:
`if 0 < len(word) <= 3 and word[0].isupper():`
- **The Issue**: Many common abbreviations exceed this limit (e.g., `approx.`, `assoc.`, `dept.`, `prof.`, `univ.`, `ext.`). 
- **Impact**: The splitter will break sentences prematurely at these abbreviations, leading to fragmented, low-context LLM requests and incorrect grammar analysis.
- **Recommendation**: Increase the character limit or implement a more comprehensive "common abbreviation" lookup list.

### 3. Trailing Whitespace Gaps
The `split_into_sentences` implementation (both the Thai/Lao/Khmer regex and the `BreakIterator` path) explicitly skips trailing whitespace between sentences.
- **The Issue**: By stripping whitespace from the work unit before sending it to the LLM, the checker loses visibility into inter-sentence formatting issues.
- **Impact**: Issues like double spaces, missing spaces, or incorrect whitespace formatting between sentences will never be identified or corrected by the AI.
- **Recommendation**: Include trailing whitespace in the sentence slice sent for analysis, while continuing to use a normalized (stripped) version for the cache key to maintain hit stability.

### 4. Serial LLM Processing in Worker
The worker processes the deduplicated batch serially, one sentence at a time.
- **The Issue**: On paragraph handoff (e.g., when opening a document or pasting text), many sentences are enqueued at once.
- **Impact**: If a paragraph has 10 sentences and each LLM call takes 2 seconds, the user will wait 20 seconds for the last squiggle to appear. Since the architecture already includes `llm_request_lane` for concurrency management, this is a missed opportunity for performance.
- **Recommendation**: Utilize a thread pool within the worker batch to process multiple uncached sentences in parallel (respecting the `llm_request_lane` limits).

### 5. Architectural Desync (Merge Conflict Risk)
The WIP branch was branched from a state prior to the major `plugin/framework` refactoring.
- **The Issue**: It moves several files (like `dialogs.py`, `document.py`, `legacy_ui.py`) *back into* the `framework` directory that were recently moved *out* of it on the `main` branch.
- **Impact**: A direct merge will be highly problematic and will revert the "lean framework" progress. 
- **Recommendation**: Rebase the WIP branch onto `main` and resolve the directory structure conflicts before attempting to land the sentence-caching changes.

