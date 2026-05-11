# Real-time / AI grammar checking — plan and status

**Status**: Shipped — UNO proofreader + engine + Linguistic `GrammarCheckers` XCU are bundled; **Batching Optimization** (paragraph-at-a-time) enabled.  
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

Do not add a separate sidebar polling / living-assistant grammar path. Native Writer grammar owns in-flow underlines, while the existing Chat with Document surface owns conversational and document-wide review.

### Architectural Scope: Sentence vs. Paragraph
The implementation draws heavily from `Lightproof` as a mature, robust foundation, but evolves significantly. Currently, the checker operates on a **sentence-at-a-time** basis for the native `XProofreader` surface. 

**Why Sentence-Scoped?**
- **Cost & Latency**: Sending a full paragraph every time a single character is typed is computationally expensive and introduces unnecessary latency into the foreground UI.
- **Precision**: By focusing on the sentence, the LLM can provide more accurate, localized error reports, reducing the risk of "offset hallucination" where squiggles appear in the wrong place.
- **Cacheability**: Sentence-level caching is highly effective; semantically identical sentences typed in different parts of a document re-trigger hits, whereas paragraph-level caching would be invalidated by almost any minor edit.

**The "Batching" Fix**: To optimize cost and latency, the worker now **batches multiple sentences from a paragraph** into a single LLM request.
- **Deduplication Strategy (Complete vs. Incomplete)**: To prevent different paragraphs from colliding in the queue (where multiple sentences often share a relative start offset of 0), the system uses a dual-keying strategy:
    - **Complete Sentences**: Keyed by the **sentence text hash**. This ensures every sentence in every paragraph has a unique key and survives deduplication on document load/scroll.
    - **Incomplete Sentences**: Keyed by a fixed **sentinel string** per document. This ensures that rapid typing drafts for the "active" sentence always supersede each other, preventing a flood of requests.
- **Hybrid Approach**: We still use **sentence-level caching**. If you edit one sentence in a paragraph, the cache is hit for the other sentences, and the AI is only asked about the dirty ones.
- **Batch prompt**: The system uses `GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE` to ask for a JSON array of results for a numbered list of sentences.
- **Chunking**: To avoid hitting LLM output token limits (which can cause a full batch to fail), the worker splits large sentence lists into smaller chunks defined by `doc.grammar_proofreader_batch_sentences` (default **1**, max **8**).
- **Force Single Mode**: Setting the batch size to **1** (default) disables batching entirely, forcing every sentence into its own request using the simpler single-sentence prompt.
- **Fallback**: If the LLM returns an incorrect number of results for a batch chunk, the system gracefully falls back to individual sentence processing for **only that chunk**.
- **Safety**: `GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS` (8192) still applies as a ceiling for pathological run-on text.

**The "High-Level" Strategy**
While sentence-scoped checking is excellent for localized grammar, it inherently misses global context (e.g., tone consistency or paragraph-level flow). Rather than forcing the grammar checker to handle high-level context (and increasing cost/complexity), we utilize the **Chat Sidebar + `add_comment` tool**. This allows the model to analyze wide context at once and leave copyeditor-style comments. This separation of concerns—**native squiggles for local grammar, sidebar chat for high-level editorial review**—is a deliberate design choice that optimizes performance while maintaining editorial depth. Future work may explore sliding-window paragraph analysis, but only if optimized to avoid the overhead of full-context re-submission on every edit.

---

## 2. What we shipped (native grammar)

### 2.1 Code and packaging

- **UNO component**: [`plugin/writer/locale/ai_grammar_proofreader.py`](../plugin/writer/locale/ai_grammar_proofreader.py) — `WriterAgentAiGrammarProofreader` (`unohelper` + `XProofreader`, locales, service info). Standalone entrypoint: extends `sys.path` like [`plugin/chatbot/panel_factory.py`](../plugin/chatbot/panel_factory.py) so `import plugin.*` works when LO loads the module. The service constructor must remain **`__init__(self, ctx, *args)`** because LibreOffice may instantiate proofreaders with `createInstanceWithArgumentsAndContext`.
- **Pure Python modules**: [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py) — **`GRAMMAR_REGISTRY_LOCALE_TAGS`**, UNO `Locale` ↔ BCP-47 bridging; Unicode sentence terminals, `looks_complete_sentence`, abbrev table, system prompt templates (**single and batch**), `parse_grammar_json`, `parse_grammar_batch_json`. [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py) — BreakIterator orchestration, `split_into_sentences`, offset normalization. [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) — sentence LRU + ignore rules. [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py) — `GrammarWorkItem`, `GrammarWorkQueue`, `run_llm_and_cache_batch` (handles grouping and LLM batching).
- **Registry**: [`extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu`](../extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu) — fuses `org.extension.writeragent.comp.pyuno.AiGrammarProofreader` under `GrammarCheckers` with `Locales` set to a space-separated list of BCP-47 tags (one `oor:string-list` `<value>`, matching Lightproof). Tags are defined as **`GRAMMAR_REGISTRY_LOCALE_TAGS`** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py) (same coverage as shipped gettext `locales/` plus `en-US` / `en-GB`). Must stay aligned with `getLocales()` (UNO `Locale` per tag) and `GRAMMAR_REGISTRY_LOCALE_TAGS` (unit test enforces parity). Document **regional** `CharLocale` values normalize to the canonical tag per language for cache and the LLM prompt.
- **Bundle**: [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) — `META-INF/manifest.xml` always lists the Python UNO module and `registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu` in default `make manifest` / `make build` output.

### 2.2 Configuration

- **All settings (Doc tab)**: `doc.grammar_proofreader_*` in [`plugin/doc/module.yaml`](../plugin/doc/module.yaml) — enable (default **off**), wait timeout (ms), optional model (empty = same as chat `text_model`), `doc.grammar_proofreader_batch_sentences` (default **1**, max **8**), and `doc.grammar_proofreader_pause_during_agent` (default **off**). LLM max output tokens (**2048**) and the **pathological** slice ceiling **`GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS`** are **fixed in code** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py). The Doc tab also inlines Calc’s **Max Rows Display** (`calc.max_rows_display` via `config_inline: doc` in [`plugin/calc/module.yaml`](../plugin/calc/module.yaml)).
- **Diagnostics**: logger name `writeragent.grammar` — `INFO` lines prefixed `[grammar]` for each `doProofreading` call, cache hit/miss, worker skip/supersede, LLM request/result counts, and `WARNING` with stack trace on worker failure. Ensure `init_logging` has run (first grammar call attempts it); see `writeragent_debug.log` under the LO user config directory (see AGENTS.md).

### 2.3 Runtime behavior (summary)

- **`doProofreading`** (async return path): On a **full cache miss**, WriterAgent returns with empty `aErrors` and enqueues a work item. On a **partial cache hit** (some sentences cached, some not), it **returns the cached errors immediately** (better than empty — squiggles appear for already-checked sentences) and enqueues for the remaining uncached sentences. On a **full cache hit** all errors are returned directly, no enqueue needed. It **does not** wait inside `doProofreading` or pump `processEventsToIdle()` for results. That keeps **menus and chrome responsive** while grammar runs.
- **`doc.grammar_proofreader_wait_timeout_ms`**: No longer used by the proofreader return path (reserved for possible future options or removed from UI in a later cleanup).
- **Sidebar status**: the proofreader emits `grammar:status` for meaningful phases (`start`, `request`, `complete`, `failed`, etc.). Skipped work is not reported to the status bar.
- **Concurrency / work queue**: A single persistent daemon thread (`GrammarWorkQueue` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)) drains a `queue.Queue` sequentially. The worker **batch-drains** all pending items, deduplicates them, and then **groups them by (document, locale)**.
- **Paragraph Batching (Chunked)**: Grouped sentences from the same paragraph/context are sent to the LLM in batches. To ensure the JSON response fits within model output token limits and to minimize latency, the system **chunks** these batches based on the user-configurable **`doc.grammar_proofreader_batch_sentences`** setting (hard-capped by **`GRAMMAR_BATCH_MAX_SENTENCES = 8`**). This significantly reduces latency and token overhead during full-paragraph checks or document loads when set to values > 1.
- **Enqueue-time replace-in-place (O(1) tail check)**: `GrammarWorkQueue.enqueue()` acquires `queue.Queue`'s own internal mutex (`self._q.mutex`) and checks the **last item** of the internal deque (`self._q.queue`). If it has the same `inflight_key` and the incoming item is newer (higher `enqueue_seq`), it **replaces it in place**. This efficiently collapses typing bursts into a single pending request without a loop. If no match is found at the tail, the item is appended normally.
- **Same-key newest wins + stale suppression**: Drain-time **`deduplicate_grammar_batch`** keeps, for each **`inflight_key`**, only the item with the highest **`enqueue_seq`**. 
- **`inflight_key` logic (Simplified Stable Key)**: To solve the "Paragraph Collision" problem where different paragraphs shared the same relative offset, the key is now state-aware:
    - **Complete**: `{doc_id}|{locale}|{hash(sent_text)}`. Ensures uniqueness across paragraphs and stability if the sentence is not being edited.
    - **Incomplete**: `{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING`. Ensures all partial drafts for the active typing spot supersede each other, preventing typing floods.
- **Stale guard**: `GrammarWorkQueue` performs a **pre-execute stale check** against `_latest_seq` and skips any survivor older than the latest known sequence for that key. After each LLM response returns, **`cache_put_sentence` is skipped** if a newer enqueue superseded this item during the HTTP call (`inflight_superseded`). Combined with a **1-second pause** before batch processing: the worker waits until there is a quiet period with no new queue items before draining.
- **Queue diagnostics**: Explicit queue logs for enqueue, drain batch size, dedup survivors, stale-skip, and execute; each includes `doc_id`, `inflight_key`, `enqueue_seq`, slice length, and a compact text preview to diagnose intermittent ordering issues. Out-of-order sequence detection in `enqueue` logs at ERROR level if an incoming item has a lower sequence than the latest recorded for that key.
- **Sentence-level gating**: grammar checks run when the slice looks like a complete sentence (terminal punctuation heuristic with multilingual marks such as `. ! ? … ؟ 。 ！ ？ ।`) **or** when partial text reaches `GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS` (15 non-space chars). Short incomplete fragments are skipped before cache/worker scheduling.
- **Pinned sentence text on enqueue**: Each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) carries **`proofread_sentence_text`** — the exact sentence segment chosen during `doProofreading`. The worker uses it for LLM + cache and **does not** call `split_into_sentences` again on the slice, avoiding BreakIterator disagreements between substring vs full-buffer splits.
- **Sentence cache**:
    - **In-Memory LRU**: Keyed by sentence fingerprint (locale + text hash). `MAX_CACHE_SIZE` is **2048**.
    - **Persistent Storage (SQLite)**: Stores up to **5000** sentences in `writeragent_grammar.db` in the user config directory. Includes a `last_used` timestamp.
    - **JSON Fallback**: If SQLite is unavailable, shards results into `.json` files in `writeragent_grammar_cache.d/`.
    - **Pruning**: Once per session, if the persistent cache exceeds 5000 entries, it prunes back to 4000 using LRU/mtime.
    - **Normalization**: Uses `_normalize_for_sentence_cache` so trailing whitespace and redundant punctuation share keys. Errors are clipped to the canonical length.
    - **Incomplete-prefix compaction**: Walks the `OrderedDict` newest-first to evict strict-prefix incomplete predecessors during typing, preventing memory bloat.
    - **Memory Warm-up**: `cache_get_sentence` promotes persistence hits to the memory LRU cache via `_populate_memory_cache_only`. This ensures subsequent re-traversals of the same sentence are handled in memory without repeated disk I/O.
    - **UI Responsiveness**: Persistence writes in `cache_put_sentence` are performed outside the global `_CACHE_LOCK`, ensuring slow disk I/O does not block the foreground proofreading pass.

  **Incomplete-prefix compaction:** See **#6: Cache Prefix Compaction** (§6 Code Quality → 7.2 Medium Priority) — newest-first bounded scan, locale-only scan budget, evict all matching incomplete strict-prefix predecessors per put.
- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/framework/client/llm_client.py) with `response_format={"type":"json_object"}` on the OpenAI-compatible path (Together, OpenRouter, etc.; see docstring on `make_chat_request`), a system prompt (**`GRAMMAR_SYSTEM_PROMPT_TEMPLATE`** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py)) requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}` (schema description in English) plus the **document language** (BCP-47 and English name from the registry), and user message the **checked sentence text** for that worker item (one sentence per request in normal prose). The prompt explicitly asks for errors in the order they appear. For threshold-allowed partial slices, the prompt adds a conservative note that input may be partial. Parser: [`parse_grammar_json`](../plugin/writer/locale/grammar_proofread_locale.py) uses `safe_json_loads` then `json_repair` (with logging) when needed.
- **Offset Normalization**: `normalize_errors_for_text` uses **`search_pos` tracking** to handle multiple occurrences of the same erroneous text within a window. If ordered scan fails and a global `find` matches **before** `search_pos`, that item is **skipped** (avoids anchoring duplicate substrings to the wrong occurrence).
- **Traversal whitespace**: `_apply_proofreading_end_positions` and initial empty-result advancement use Unicode **`str.isspace()`**, not ASCII space only, so tabs/NBSP between sentences advance Writer’s next position correctly.
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### 2.3.1 Why `enqueue_seq` exists (queue FIFO is not enough)

**Terminology.** The shipped code uses a **global integer counter** (`next_enqueue_seq()` / `_ENQUEUE_SEQ` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)), incremented when a cache miss enqueues work; each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) stores it as **`enqueue_seq`**. This is **not** the same as `time.monotonic()` — that clock is used elsewhere only for **elapsed milliseconds** on LLM requests (status/diagnostics), not for ordering queue items.

**Why not rely only on “everything goes through `queue.Queue`”?** A FIFO queue orders **`get()` dequeue order** among objects that are actually retrieved in sequence. The grammar worker deliberately does **more** than strict FIFO:

1. **Tail replace-in-place** — For the same `inflight_key`, a newer item can **overwrite** the last slot of the internal deque without establishing a simple FIFO relationship to items already consumed in an **earlier** batch. Queue position alone does not record “this snapshot superseded that one” across batches.

2. **Batch drain + `deduplicate_grammar_batch`** — The worker collects multiple `get()` results into one batch, then for each **`inflight_key`** keeps only the highest **`enqueue_seq`**.

3. **`_latest_seq` / pre-execute stale skip** — Before calling the LLM, the worker asks whether a **newer** enqueue has already been recorded for that `inflight_key`. **`Post-LLM`**: re-check before `cache_put_sentence`; if superseded during the HTTP call, skip the cache write.

So **`enqueue_seq` is a generation stamp for supersede/dedup semantics**, not a substitute for the queue. Something must play that role whenever work is merged, replaced, or skipped outside pure FIFO.

**Alternatives (same role, different representation):**

| Approach | Notes |
|----------|--------|
| **Per-`inflight_key` counter** | Bump only when enqueueing for that document+locale key. Same semantics as today’s global counter for same-key comparisons; avoids mixing sequence space across unrelated documents (clearer for logs and reasoning). |
| **Enqueue-time monotonic value** | e.g. `time.monotonic()` at enqueue as the order key. Requires discipline if two enqueues share an identical timestamp resolution; still needs to be stored on each `GrammarWorkItem` and mirrored (like `_latest_seq`) for stale checks. |
| **Post-LLM staleness guard** | **Shipped:** `inflight_superseded(inflight_key, enqueue_seq)` after `chat_completion_sync` returns and before `cache_put_sentence`. |

### 2.4 Tests

- Unit: [`plugin/tests/writer/locale/test_grammar_proofread_engine.py`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py) — JSON parsing, offset normalization, sentence cache roundtrip, trailing whitespace cache normalization, ignore rules, overlap expansion.
- Unit (work queue dedup): [`plugin/tests/writer/locale/test_grammar_work_queue.py`](../plugin/tests/writer/locale/test_grammar_work_queue.py) — same-key supersede, reverse-prefix chain reproducer, distinct `inflight_key` survival, **paragraph batching success, and LLM result-mismatch fallback**.
- Unit (queue / worker): [`plugin/tests/writer/locale/test_ai_grammar_proofreader_worker.py`](../plugin/tests/writer/locale/test_ai_grammar_proofreader_worker.py) — `GrammarWorkQueue` stale detection, legacy Lightproof finalize regression helper, pinned `proofread_sentence_text` worker path.
- UNO (native runner): [`plugin/tests/writer/locale/test_grammar_uno.py`](../plugin/tests/writer/locale/test_grammar_uno.py) — cache path, `ignoreRule`, incremental overlap (relocated paths; run via `plugin.testing_runner`).

### 2.5 Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on the **Doc** tab; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | `doProofreading` does **not** wait on the main thread for LLM results (avoids dead menus while grammar runs). HTTP/LLM runs on a background worker; underlines update on a **later** proofreading pass when the sentence cache is ready. |
| Stale underlines | Sentence cache (locale + sentence text fingerprint) plus sequential work queue with same-key supersede, pre-execute stale skip, and post-LLM cache-write guard. **Cache hit** → immediate errors; **miss** → empty return once, queue worker fills cache for the next pass. See §5 for evolving this. |
| Concurrent chat agent | Optional guard (`doc.grammar_proofreader_pause_during_agent`) can skip grammar worker calls while chat/agent sends are active; grammar and chat/agent LLM requests also share one in-process request lane to avoid overlap races. |

---

## 3. Lightproof-inspired optimizations

The native grammar checker pairs **sentence-bound work units** with **sentence-level caching** (Lightproof-inspired scheduling ideas, evolved):

1.  **Sentence-sized scheduling**: `doProofreading` maps LibreOffice’s call to **whole sentences** in `aText` (paragraph pass vs incremental overlap). `ProofreadingResult` traversal positions follow the **union of checked sentences** via `_apply_proofreading_end_positions` — no fixed 500-character proofread window.
2.  **Sentence-level caching**: The old slice-level cache (`_proofread_cache` / `make_cache_key` / `cache_get` / `cache_put` keyed by doc + locale + fingerprint + bounds) has been **removed**. All caching now goes through the **sentence-level cache** (`cache_get_sentence` / `cache_put_sentence` in [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py)). Normalization uses `_normalize_for_sentence_cache` so that trailing whitespace is stripped **and** any punctuation after the *first* sentence terminator is ignored for the cache key (`"Hello."` and `"Hello..."` share a key; `"Hello?"` and `"Hello?..."` share one; but `"Hello?"` vs `"Hello."` remain distinct). Errors are clipped to the canonical length. This means semantically equivalent sentence text anywhere in the document reuses the same errors regardless of document position or trailing punctuation style. See §2.3 "Sentence cache" for the full lookup/storage behavior.

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
6.  **Persistent Cache**: **Shipped:** SQLite-based persistent cache in `writeragent_grammar.db` with sharded JSON fallback. Limits storage to 5000 entries with session-start pruning.
7.  **Document-generation invalidation**: If LO exposes a revision counter, generation id, or “document modified” tick, fold it into the cache key or force miss when the full buffer changes even if a slice string matches (reduces risk of stale absolute offsets after edits above the span).
8.  **Shared policy with chat**: Baseline shipped: optional pause-during-agent setting + shared in-process LLM request lane. Future expansion: endpoint-aware policy (per provider/model), richer status UX, and adaptive queue/backoff.
9.  **Smaller / faster grammar model**: Route grammar-only traffic to a cheaper or local model by default; keep “same as chat” as an override (already partially supported via `grammar_proofreader_model`).
10. **Prompt and schema hardening**: Few-shot examples for edge cases (quotes, lists, track changes); strict JSON recovery; optional `response_format` where the API supports it.
11. **Paragraph / traversal tuning**: If Writer scheduling or underlines misbehave on some LO versions, compare our sentence selection vs stock Lightproof’s `len(rText)` and adjust `_apply_proofreading_end_positions` / overlap rules only.
12. **Ignore rules & parity**: Persist `ignoreRule` across sessions; locale-specific ignores if the API evolves.
13. **Observability**: Debug metrics (cache hit rate, worker supersede count, p50/p95 latency from schedule → `cache_put`) behind a verbose flag for field debugging.
14. **Accessibility / UX copy**: Clear user-facing text that grammar is **asynchronous** (squiggles after pause); link to Writing aids selection when multiple proofreaders exist.
15. **LanguageTool-class local checking (research)**: Phased roadmap for a **Python-first** checker consuming LT-open rule data toward LanguageTool-grade quality over time, **without JVM** in-stack (`nlprule`/fork accelerator optional). See [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md).
16. **Parallel grammar worker (optional)**: On paragraph handoff many sentences enqueue at once; processing remains **one batch after another** on a single worker thread. Future option: limited parallelism for **distinct** documents within one batch while respecting `llm_request_lane`.
17. **Batched LLM Requests (Latency/Cost) — SHIPPED**: Paragraph-at-a-time batching is now the default path for grouped sentence enqueues.
18. **Queue LIFO/Visibility Priority**: The single sequential worker can get clogged if a user scrolls through a long document (enqueueing many sentences) and then starts typing a new sentence. Consider a LIFO-ish priority queue or a mechanism to flush/deprioritize work items that are no longer visible on screen, ensuring the currently active typing area gets checked first.

--- 

## 6. Code Quality Improvements (Non-Feature)

This section tracks **code health** improvements: simplification, robustness, and maintainability **without** adding user-facing features. These are candidate tasks for cleanup sprints or when refactoring adjacent areas.

### 7.1 High Priority (Correctness & Robustness)

| # | Task | File | Lines | Impact | Effort |
|---|------|------|-------|--------|--------|
| 1 | Reduce exception swallowing in `doProofreading` | `ai_grammar_proofreader.py` | 310-510 | Hard to debug failures; masks real problems | 30 min |
| 2 | Extract `time.sleep` into patchable helper | `ai_grammar_proofreader.py` | 22-24 | Tests shouldn't patch internal `time` import | 10 min |
| 3 | Remove duplicate `unohelper` import | `ai_grammar_proofreader.py` | 28, 470 | First import unused; shadows second | 2 min |
| 4 | Use `re.escape()` for `_sterm_class` regex | `grammar_proofread_locale.py` | 388-391 | **Done** | 5 min |
| 5 | Add HTTP 429 / exponential backoff | `grammar_work_queue.py` | `run_llm_and_cache_batch` | Prevents API flooding during rapid typing or doc load | 45 min |

**Details:**

**#1: Exception Swallowing**
The `doProofreading` method has ~6 nested try-except blocks, many of which only log warnings and continue. This masks real problems like config read failures or UNO struct creation errors. Consider extracting helper functions (`_safe_init_logging`, `_safe_get_config`, `_safe_build_result`) to reduce nesting and make failures more explicit.

**#2: Testability**
Tests currently patch `time.sleep` at the module level. Extract into `_sleep(seconds)` helper that tests can patch instead.

**#3: Dead Import**
Line 28 imports `unohelper` but it's unused. The actual import is at line 470. Remove line 28.

**#4: Regex Safety**
The `_sterm_class` regex is built by manual escaping which is fragile and misses many regex metacharacters. Use `re.escape(_sterm_chars)` instead of hand-rolled `.replace()` chain.

**#5: HTTP 429 / Backoff**
Track last request time per endpoint and implement exponential backoff (base 1s, cap at 60s) to prevent rate-limiting errors during bursts of typing or document scroll events.

---

### 7.2 Medium Priority (Simplification & Clarity)

| # | Task | File | Lines | Impact | Effort |
|---|------|------|-------|--------|--------|
| 6 | Cache prefix compaction (incomplete sentence LRU) — **shipped** | `grammar_proofread_cache.py` | ~133–164 (`cache_put_sentence`) | Supersedes old “one predecessor / awkward scan” issues; see **#6** details | Done |
| 8 | Pre-compile regex patterns | `grammar_proofread_text.py` | Various | Minor performance + clarity | 10 min |
| 9 | Unify `is_stale()` and `inflight_superseded()` | `grammar_work_queue.py` | ~100-110 | DRY; both check seq against latest | 15 min |
| 10 | Add viewport priority to queue | `grammar_work_queue.py` | `GrammarWorkItem`, `deduplicate_grammar_batch` | Active typing area jumps queue ahead of scroll/chrome work | 60 min |

**Details:**

**#6: Cache Prefix Compaction**

**Problem (historical):** Each incomplete fragment (`The`, `The qu`, …) gets its own fingerprint key. Without compaction, incremental typing would leave **many** LRU slots for one logical sentence. Earlier implementations removed **at most one** predecessor per put (spurious `break`), applied the bounded-scan counter in an awkward order relative to locale filtering, or materialized `list(_SENTENCE_CACHE.items())[::-1]` on every put.

**Fix shipped:** [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) — on **`cache_put_sentence`** when the normalized text is still **incomplete** (no sentence terminator), walk the sentence `OrderedDict` **newest-first** via `reversed(_SENTENCE_CACHE.items())` (no list copy; Python 3.11+ reverses the items view) so the stub the user is replacing is usually hit **early** when repeatedly typing in the same sentence. Apply **`sentence_cache_key_prefix(locale)`** before advancing **`MAX_RECENT_INCOMPLETE_SCAN` (10)** so the budget counts **only** keys for that locale (`sent|<locale>|…`). Collect **all** strict-prefix incomplete predecessors that match within that window and remove them after the scan. Eviction uses **`should_evict_incomplete_prefix_predecessor`** — **complete** sentences are never removed by this path. **Not** the same policy as queue **`deduplicate_grammar_batch`** (no cross–sentence-start prefix relation across different `inflight_key`s).

**Regression tests:** [`test_sentence_cache_incomplete_prefix_compaction`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py), [`test_sentence_cache_locale_isolation`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py).

**#8: Regex Pre-compilation**
Patterns like `GRAMMAR_WHITESPACE_RUN_RE` are defined as raw strings then used in `re.compile()` calls. Could pre-compile at module level.

---

### 7.3 Low Priority (Nice-to-Have)

| # | Task | File | Impact |
|---|------|------|--------|
| 9 | Use `@dataclass` for ProofreadingResult helper | `ai_grammar_proofreader.py` | Cleaner code |
| 10 | Add type hints for UNO struct returns | Various | Better IDE support |
| 11 | Unify `is_stale()` and `inflight_superseded()` | `grammar_work_queue.py` | DRY |
| 12 | Improve docstrings for complex algorithms | `grammar_proofread_text.py` | Maintainability |

**Details:**

**#10: Unify Stale Functions**
`is_stale(latest_seq: Mapping[str, int], item: GrammarWorkItem)` and `inflight_superseded(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int)` do nearly the same thing. Could be one function with overloaded signatures or a helper that takes the key extraction as a parameter.

---

### 7.4 Structural Suggestions

#### 7.4.1 Error Handling Strategy

Adopt a **tiered error handling** approach instead of the current "log and continue everywhere" pattern:

| Level | Action | Example |
|-------|--------|---------|
| **Fatal** | Raise / return None | UNO module missing, `createUnoStruct` fails |
| **Recoverable** | Log ERROR + return empty/default | Config read fails, locale not supported |
| **Diagnostic** | Log INFO/DEBUG + continue | Cache miss, queue deduplication |

Currently, too many recoverable errors are silently swallowed with just a warning log.

#### 7.4.2 Logging Discipline

- Use **structured logging** consistently (the `grammar_obs` calls are good)
- Avoid **duplicate log messages** (same event logged at different levels)
- Use appropriate log levels:
  - `DEBUG`: Per-sentence cache hits/misses, queue operations
  - `INFO`: High-level state changes (enabled/disabled, worker start/stop)
  - `WARNING`: Recoverable failures (config errors, partial failures)
  - `ERROR`: Unrecoverable failures that degrade functionality

#### 7.4.3 Testing Considerations

- **Avoid patching internal imports** in tests (e.g., don't patch `time.sleep` at module level)
- **Extract side effects** into injectable helpers
- **Prefer pure functions** where possible (e.g., `deduplicate_grammar_batch`, `should_evict_incomplete_prefix_predecessor`)

---

### 7.5 Quick Wins (Under 5 minutes)

| # | Task | File | Lines | Status | Effort |
|---|------|------|-------|--------|--------|
| 1 | Remove dead import: `import unohelper` at line 28 (shadowed by line 470) | `ai_grammar_proofreader.py` | 28 | **Done** | 2 min |
| 2 | Fix broken `_persistence_lock` (`os.fork()` → remove) | `grammar_persistence.py` | 227 | **Done** | 2 min |
| 3 | Fix regex escaping: use `re.escape(_sterm_chars)` | `grammar_proofread_locale.py` | 388-391 | **Done** | 5 min |
| 4 | Extract `time.sleep()` into patchable `_sleep()` helper | `ai_grammar_proofreader.py` | 22-24 | **Done** | 10 min |
| 5 | Pre-compile regex patterns (`GRAMMAR_WHITESPACE_RUN_RE`, etc.) | `grammar_proofread_text.py` | Various | **Done** | 10 min |

### Docs / agents

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).
- Optional non-LLM checker roadmap grounded in LT behavior: [`languagetool-local-parity-phased-plan.md`](languagetool-local-parity-phased-plan.md).


---

### Design note: `deduplicate_grammar_batch` (cross-sentence prefix bug)

**Problem:** An older implementation added a *second* dedup step that grouped queue items by `(doc_id, locale)` and dropped items whose **slice text** was in a **string prefix** relation with another item (newest `enqueue_seq` wins). That matches typing inside **one** sentence, but `inflight_key` is already `doc|locale|sentence_start` — one key per sentence. **Different sentences** in the same paragraph can still have texts where one is a prefix of the other (e.g. first sentence `No.` and a later sentence `No problem today.`). Cross-key prefix logic **dropped the shorter sentence’s work** and skipped a valid LLM check.

**Fix shipped:** `deduplicate_grammar_batch` only keeps, for each **`inflight_key`**, the item with the highest **`enqueue_seq`**. No text-prefix pass across distinct keys. Same-sentence typing is covered by the same `inflight_key` plus enqueue tail-replace.

**Other ways to fix** (if you revisit this — avoid regressions):

| Approach | Notes |
|----------|--------|
| Prefix-newest-wins **only for the same `inflight_key`** | Narrow the old idea to the typing timeline only; often equivalent to “one survivor per key” after step 1. |
| **Span-aware** prefix rules | Drop prefix-related items only when `n_start`/`n_end` ranges overlap (same physical sentence), not when offsets differ. |
| **No cross-key text comparison** | Rely on `inflight_key` + tail-replace only (current direction). |

**Regression test:** [`test_two_sentences_string_prefix_collision_both_survive`](../plugin/tests/writer/locale/test_grammar_work_queue.py). Implementation notes are in **comments directly above** [`deduplicate_grammar_batch`](../plugin/writer/locale/grammar_work_queue.py) in that file (not in the module docstring).
