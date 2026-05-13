# Real-time / AI grammar checking — plan and status

**Status**: Shipped — UNO proofreader + engine + Linguistic `GrammarCheckers` XCU are bundled; batching (paragraph-at-a-time) is enabled and configurable.  
**Authors**: WriterAgent Team  
**Audience**: Developers and PMs aligning on **native Writer linguistic grammar** vs optional **sidebar chat** (different surfaces, different jobs).

### How to use this document

| Section | Use it when you need… |
|--------|------------------------|
| **Concepts and behavior** | UNO proofreader API basics, product boundaries, sentence vs paragraph scheduling |
| **Shipped implementation reference** | Module map, settings keys, runtime/cache/queue behavior, tests |
| **Completed milestones** vs **Open backlog** | What is done vs what remains (single source for work items) |
| **Appendices** | Historical bug write-ups and doc-maintenance pointers |

### At a glance

- **Native grammar** is implemented as an `XProofreader` service with Lightproof-style registry (`LinguisticWriterAgentGrammar.xcu`); users enable LLM work on the **Doc** tab and pick the proofreader under Writing aids.
- **Batching** groups sentences from the same paragraph into chunked LLM requests; batch size is capped (`doc.grammar_proofreader_batch_sentences`, max 8).
- **Sidebar chat** is separate: use it for explanations, rewrites, and editorial comment tools—not as a second linguistic pipeline.

---

## Concepts and behavior

### Tutorial: LibreOffice grammar proofreader API

This section explains the LibreOffice Linguistic2 proofreader API as WriterAgent uses it. The key idea is that Writer does **not** ask a proofreader to scan the whole document. Writer calls a registered `XProofreader` with a text buffer and offset hints, then expects a `ProofreadingResult` containing errors whose positions are relative to that buffer.

#### The moving parts

- **Registration**: WriterAgent registers `WriterAgentAiGrammarProofreader` as a `com.sun.star.linguistic2.Proofreader` service in the extension registry. LibreOffice discovers it through `GrammarCheckers` XCU configuration and the component implementation name.
- **Locale support**: LibreOffice asks `hasLocale()` and `getLocales()` to decide whether this checker applies to the current document language. WriterAgent normalizes UNO locales like `en_US` into BCP-47 tags like `en-US` for cache keys and LLM prompts.
- **Proofreading entry point**: LibreOffice calls `doProofreading(...)`. This is the main hot path. It may be called frequently while typing, while opening a document, when visible text changes, or when the grammar dialog asks for results.
- **Result object**: The proofreader returns a `com.sun.star.linguistic2.ProofreadingResult`. It contains the checked text, sentence boundary fields, and a tuple of `SingleProofreadingError` objects. Writer uses those error spans to draw proofreading underlines.

#### `doProofreading` parameters

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

#### `ProofreadingResult` fields Writer cares about

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

#### How WriterAgent adapts a synchronous API to async LLM work

`doProofreading` is synchronous from LibreOffice's point of view: Writer calls it and expects a `ProofreadingResult` back now. LLM calls are too slow for that foreground path, so WriterAgent uses a cache-first strategy:

1. Normalize the locale and choose the text span to check.
2. Split that span into sentence candidates.
3. Return cached sentence errors immediately when available.
4. For uncached sentences, enqueue background work and return an empty or partial result.
5. On a later LibreOffice proofreading pass, the sentence cache is warm and `doProofreading` returns real errors synchronously.

This is why the API result boundaries matter so much. They tell Writer what span was handled now, while the queue decides what LLM work will become available for a future pass.

#### Paragraph handoff vs typing churn

LibreOffice can call the same API in different situations:

- **Paragraph-scale handoff**: `aText` contains multiple stable sentence candidates, often an entire paragraph. `nStartOfSentencePosition` is usually `0` or a real sentence boundary. `nSuggestedBehindEndOfSentencePosition` may only describe LibreOffice's first sentence guess, but `aText` has more sentences after it. WriterAgent should split the handed-in span and enqueue every uncached sentence candidate so the paragraph is fully covered.
- **Incremental typing span**: `aText` may still be paragraph-like, but `nStartOfSentencePosition` and `nSuggestedBehindEndOfSentencePosition` identify the active range Writer is probing while the user edits. WriterAgent should map that active range to the containing sentence and enqueue only that sentence. Newer versions of the same sentence then replace older queued work through `inflight_key` and `enqueue_seq`.

The design target is therefore **sentence-sized work**, not "only the first sentence." A paragraph can produce multiple sentence-sized queue items; active typing should collapse to the one sentence currently changing.

---

### Native grammar vs chat (do not conflate)

| Surface | UX | Role |
|--------|-----|------|
| **Native Writer grammar (Linguistic2)** | Same as other grammar extensions: Writer's grammar pass, underlines, grammar dialog. Uses `XProofreader` + `Linguistic` / `GrammarCheckers` registry. | **Shipped / experimental** — Python `XProofreader` + Lightproof-style XCU are in the default OXT; users enable LLM work on the **Doc** tab and pick the active proofreader under Writing aids. Earlier native crashes were fixed by accepting extra UNO constructor args (`__init__(self, ctx, *args)`). |
| **Chat with Document (sidebar)** | Multi-turn chat with document context and tools — not a duplicate linguistic pipeline. | Use this when you want **explanations, rewrites, or whole-paragraph help**; it does not replace underlined in-flow proofreading. |

Do not add a separate sidebar polling / living-assistant grammar path. Native Writer grammar owns in-flow underlines, while the existing Chat with Document surface owns conversational and document-wide review.

---

### Architectural scope: sentence vs paragraph

The implementation draws heavily from `Lightproof` as a mature, robust foundation, but evolves significantly. Currently, the checker operates on a **sentence-at-a-time** basis for the native `XProofreader` surface.

**Why sentence-scoped?**

- **Cost & latency**: Sending a full paragraph every time a single character is typed is computationally expensive and introduces unnecessary latency into the foreground UI.
- **Precision**: By focusing on the sentence, the LLM can provide more accurate, localized error reports, reducing the risk of "offset hallucination" where squiggles appear in the wrong place.
- **Cacheability**: Sentence-level caching is highly effective; semantically identical sentences typed in different parts of a document re-trigger hits, whereas paragraph-level caching would be invalidated by almost any minor edit.

**Batching**: To optimize cost and latency, the worker **batches multiple sentences from a paragraph** into a single LLM request when configured.

- **Deduplication strategy (complete vs incomplete)**: To prevent different paragraphs from colliding in the queue (where multiple sentences often share a relative start offset of 0), the system uses a dual-keying strategy:
    - **Complete sentences**: Keyed by the **sentence text hash**. This ensures every sentence in every paragraph has a unique key and survives deduplication on document load/scroll.
    - **Incomplete sentences**: Keyed by a fixed **sentinel string** per document. This ensures that rapid typing drafts for the "active" sentence always supersede each other, preventing a flood of requests.
- **Hybrid approach**: We still use **sentence-level caching**. If you edit one sentence in a paragraph, the cache is hit for the other sentences, and the AI is only asked about the dirty ones.
- **Batch prompt**: The system uses `GRAMMAR_BATCH_SYSTEM_PROMPT_TEMPLATE` to ask for a JSON array of results for a numbered list of sentences.
- **Chunking**: To avoid hitting LLM output token limits (which can cause a full batch to fail), the worker splits large sentence lists into smaller chunks defined by `doc.grammar_proofreader_batch_sentences` (default **1**, max **8**).
- **Force single mode**: Setting the batch size to **1** (default) disables batching entirely, forcing every sentence into its own request using the simpler single-sentence prompt.
- **Fallback**: If the LLM returns an incorrect number of results for a batch chunk, the system gracefully falls back to individual sentence processing for **only that chunk**.
- **Safety**: `GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS` (8192) still applies as a ceiling for pathological run-on text.

**High-level editorial context**

While sentence-scoped checking is excellent for localized grammar, it inherently misses global context (e.g., tone consistency or paragraph-level flow). Rather than forcing the grammar checker to handle high-level context (and increasing cost/complexity), we utilize the **Chat Sidebar + `add_comment` tool**. This allows the model to analyze wide context at once and leave copyeditor-style comments. This separation of concerns — **native squiggles for local grammar, sidebar chat for high-level editorial review** — is a deliberate design choice. Future work may explore sliding-window paragraph analysis, but only if optimized to avoid the overhead of full-context re-submission on every edit.

---

### Internationalization and Locale Support

WriterAgent is designed to be a "world-class" checker, leveraging the LLM's multilingual capabilities alongside native LibreOffice linguistic tools.

#### Strategy: "Native splitting + LLM checking"

1.  **Sentence Splitting**: We use `com.sun.star.i18n.BreakIterator` for standard scripts. This is the "gold standard" for locale-aware splitting in LibreOffice.
2.  **Special Cases**: 
    - **Thai/Lao/Khmer**: Use whitespace-run splitting as a fallback since standard sentence BI is often unreliable for these scripts.
    - **Abbreviations**: A dynamic alpha-count heuristic (`word_before_period_is_abbrev`) handles `Dr.`, `U.S.A.`, etc., across many languages without static lists.
3.  **Prompting**: The system prompt is in English but explicitly identifies the target language by name and BCP-47 tag. It instructs the LLM to provide the "reason" in the same language as the text.
4.  **Normalization**: Locales like `de-AT` are normalized to `de-DE` for the sentence cache. This ensures that common phrases ("Guten Tag.") share a cache entry regardless of regional settings, though it can be opted-out if regional nuances are critical.

#### Known Nuances

| Language / Script | Logic | Note |
|-------------------|-------|------|
| **Latin / Cyrillic / Greek** | BreakIterator + Heuristic | Standard punctuation-based splitting. |
| **CJK (Japanese/Chinese/Korean)** | BreakIterator | Uses ideographic full stops (`。`) and other full-width terminals. |
| **Thai / Lao / Khmer** | Whitespace-run split | No spaces between words; spaces used as sentence/phrase breaks. |
| **Arabic / Hebrew / Urdu** | BreakIterator | RTL scripts with specific terminals (`؟`, `۔`). |
| **German / French / etc.** | Abbrev Heuristic | Handles ordinals (German `1.`) as abbreviations to prevent splitting. |

---


### Design principles (Lightproof-inspired)

The native grammar checker pairs **sentence-bound work units** with **sentence-level caching** (Lightproof-inspired scheduling ideas, evolved):

1. **Sentence-sized scheduling**: `doProofreading` maps LibreOffice's call to **whole sentences** in `aText` (paragraph pass vs incremental overlap). `ProofreadingResult` traversal positions follow the **union of checked sentences** via `_apply_proofreading_end_positions` — no fixed 500-character proofread window.
2. **Sentence-level caching**: The old slice-level cache (`_proofread_cache` / `make_cache_key` / `cache_get` / `cache_put` keyed by doc + locale + fingerprint + bounds) has been **removed**. All caching now goes through the **sentence-level cache** (`cache_get_sentence` / `cache_put_sentence` in [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py)). Normalization uses `_normalize_for_sentence_cache` so that trailing whitespace is stripped **and** any punctuation after the *first* sentence terminator is ignored for the cache key (`"Hello."` and `"Hello..."` share a key; `"Hello?"` and `"Hello?..."` share one; but `"Hello?"` vs `"Hello."` remain distinct). Errors are clipped to the canonical length. Semantically equivalent sentence text anywhere in the document reuses the same errors regardless of document position or trailing punctuation style. See **Sentence cache** under [Runtime behavior](#runtime-behavior) for lookup/storage behavior.

---

## Shipped implementation reference

### Code and packaging

- **UNO component**: [`plugin/writer/locale/ai_grammar_proofreader.py`](../plugin/writer/locale/ai_grammar_proofreader.py) — `WriterAgentAiGrammarProofreader` (`unohelper` + `XProofreader`, locales, service info). Standalone entrypoint: extends `sys.path` like [`plugin/chatbot/panel_factory.py`](../plugin/chatbot/panel_factory.py) so `import plugin.*` works when LO loads the module. The service constructor must remain **`__init__(self, ctx, *args)`** because LibreOffice may instantiate proofreaders with `createInstanceWithArgumentsAndContext`.
- **Pure Python modules**: [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py) — **`GRAMMAR_REGISTRY_LOCALE_TAGS`**, UNO `Locale` ↔ BCP-47 bridging; Unicode sentence terminals, `looks_complete_sentence`, abbrev table, system prompt templates (**single and batch**), `parse_grammar_json`, `parse_grammar_batch_json`. [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py) — BreakIterator orchestration, `split_into_sentences`, offset normalization. [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) — sentence LRU + ignore rules. [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py) — `GrammarWorkItem`, `GrammarWorkQueue`, `run_llm_and_cache_batch` (handles grouping and LLM batching).
- **Registry**: [`extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu`](../extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu) — fuses `org.extension.writeragent.comp.pyuno.AiGrammarProofreader` under `GrammarCheckers` with `Locales` set to a space-separated list of BCP-47 tags (one `oor:string-list` `<value>`, matching Lightproof). Tags are defined as **`GRAMMAR_REGISTRY_LOCALE_TAGS`** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py) (same coverage as shipped gettext `locales/` plus `en-US` / `en-GB`). Must stay aligned with `getLocales()` (UNO `Locale` per tag) and `GRAMMAR_REGISTRY_LOCALE_TAGS` (unit test enforces parity). Document **regional** `CharLocale` values normalize to the canonical tag per language for cache and the LLM prompt.
- **Bundle**: [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) — `META-INF/manifest.xml` always lists the Python UNO module and `registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu` in default `make manifest` / `make build` output.

### Configuration

- **All settings (Doc tab)**: `doc.grammar_proofreader_*` in [`plugin/doc/module.yaml`](../plugin/doc/module.yaml) — enable (default **off**), wait timeout (ms), optional model (empty = same as chat `text_model`), `doc.grammar_proofreader_batch_sentences` (default **1**, max **8**), and `doc.grammar_proofreader_pause_during_agent` (default **off**). LLM max output tokens (**2048**) and the **pathological** slice ceiling **`GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS`** are **fixed in code** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py). The Doc tab also inlines Calc's **Max Rows Display** (`calc.max_rows_display` via `config_inline: doc` in [`plugin/calc/module.yaml`](../plugin/calc/module.yaml)).
- **Diagnostics**: logger name `writeragent.grammar` — `INFO` lines prefixed `[grammar]` for each `doProofreading` call, cache hit/miss, worker skip/supersede, LLM request/result counts, and `WARNING` with stack trace on worker failure. Ensure `init_logging` has run (first grammar call attempts it); see `writeragent_debug.log` under the LO user config directory (see [`AGENTS.md`](../AGENTS.md)).

### Runtime behavior

#### Foreground path (`doProofreading`) and UI hooks

- **`doProofreading`** (async return path): On a **full cache miss**, WriterAgent returns with empty `aErrors` and enqueues a work item. On a **partial cache hit** (some sentences cached, some not), it **returns the cached errors immediately** (better than empty — squiggles appear for already-checked sentences) and enqueues for the remaining uncached sentences. On a **full cache hit** all errors are returned directly, no enqueue needed. It **does not** wait inside `doProofreading` or pump `processEventsToIdle()` for results. That keeps **menus and chrome responsive** while grammar runs.
- **`doc.grammar_proofreader_wait_timeout_ms`**: No longer used by the proofreader return path (reserved for possible future options or removed from UI in a later cleanup).
- **Sidebar status**: the proofreader emits `grammar:status` for meaningful phases (`start`, `request`, `complete`, `failed`, etc.). Skipped work is not reported to the status bar.

#### Worker thread, quiet period, and batching

- **Concurrency / work queue**: A single persistent daemon thread (`GrammarWorkQueue` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)) drains a `queue.Queue` sequentially. The worker **batch-drains** all pending items, deduplicates them, and then **groups them by (document, locale)**.
- **Quiet period**: The worker uses `queue.Queue.get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)` (see `grammar_proofread_locale.py`) so batches wait for a short idle window rather than spamming the LLM on every micro-edit.
- **Paragraph batching (chunked)**: Grouped sentences from the same paragraph/context are sent to the LLM in batches. Chunks respect **`doc.grammar_proofreader_batch_sentences`** (hard-capped by **`GRAMMAR_BATCH_MAX_SENTENCES = 8`**). This reduces latency and token overhead during full-paragraph checks or document loads when set to values greater than 1.

#### Tail enqueue, dedup keys, stale detection, diagnostics

- **Enqueue-time replace-in-place (O(1) tail check)**: `GrammarWorkQueue.enqueue()` acquires `queue.Queue`'s own internal mutex (`self._q.mutex`) and checks the **last item** of the internal deque (`self._q.queue`). If it has the same `inflight_key` and the incoming item is newer (higher `enqueue_seq`), it **replaces it in place**. This efficiently collapses typing bursts into a single pending request without a loop. If no match is found at the tail, the item is appended normally.
- **Same-key newest wins + stale suppression**: Drain-time **`deduplicate_grammar_batch`** keeps, for each **`inflight_key`**, only the item with the highest **`enqueue_seq`**.
- **`inflight_key` logic (stable key)**:
    - **Complete**: `{doc_id}|{locale}|{hash(sent_text)}`. Ensures uniqueness across paragraphs and stability if the sentence is not being edited.
    - **Incomplete**: `{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING`. Ensures all partial drafts for the active typing spot supersede each other, preventing typing floods.
- **Stale guard**: `GrammarWorkQueue` performs a **pre-execute stale check** against `_latest_seq` and skips any survivor older than the latest known sequence for that key. After each LLM response returns, **`cache_put_sentence` is skipped** if a newer enqueue superseded this item during the HTTP call (`inflight_superseded`).
- **Queue diagnostics**: Explicit queue logs for enqueue, drain batch size, dedup survivors, stale-skip, and execute; each includes `doc_id`, `inflight_key`, `enqueue_seq`, slice length, and a compact text preview to diagnose intermittent ordering issues. Out-of-order sequence detection in `enqueue` logs at ERROR level if an incoming item has a lower sequence than the latest recorded for that key.

#### Sentence gating and pinned text

- **Sentence-level gating**: grammar checks run when the slice looks like a complete sentence (terminal punctuation heuristic with multilingual marks such as `. ! ? … ؟ 。 ！ ？ ।`) **or** when partial text reaches `GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS` (15 non-space chars). Short incomplete fragments are skipped before cache/worker scheduling.
- **Pinned sentence text on enqueue**: Each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) carries **`proofread_sentence_text`** — the exact sentence segment chosen during `doProofreading`. The worker uses it for LLM + cache and **does not** call `split_into_sentences` again on the slice, avoiding BreakIterator disagreements between substring vs full-buffer splits.

#### Sentence splitting and abbreviation handling

- **Sentence splitting**: Uses LibreOffice's UNO `com.sun.star.i18n.BreakIterator` as the primary sentence boundary detector. This provides locale-aware sentence splitting for all supported scripts (Latin, Cyrillic, CJK, Arabic, etc.).
- **Abbreviation detection**: Dynamic rule-based approach in [`word_before_period_is_abbrev()`](../plugin/writer/locale/grammar_proofread_locale.py) — **no hard-coded lists**. Returns the **alpha character count** (1-6) for text abbreviations (Unicode-aware via `isalpha()`; internal punctuation like dots in `U.S.A.` does **not** count toward the limit), returns **1** for pure numbers (any length, with separators), or **0** for non-abbreviations.
- **Abbreviation extension logic**: When BreakIterator identifies a period as a potential sentence boundary, the code checks if the preceding word is an abbreviation (alpha count > 0). If so, it skips past the period to `i + 1`, advances past any whitespace, then calls `BreakIterator.endOfSentence()` from that clean position to find the true sentence end. This avoids infinite loops while correctly handling cases like `Dr. Johnson asked...` as a single sentence.
- **Why not spaCy**: Evaluated spaCy for abbreviation detection but rejected it because its tokenization data and models contained email addresses, personal data, and other extraneous content. The dynamic character-counting approach is simpler, more maintainable, privacy-preserving, and works universally across all scripts without large static tables or external dependencies.

#### Sentence cache

- **In-memory LRU**: Keyed by sentence fingerprint (locale + text hash). `MAX_CACHE_SIZE` is **2048**.
- **Persistent storage (SQLite)**: Stores up to **5000** sentences in `writeragent_grammar.db` in the user config directory. Includes a `last_used` timestamp.
- **JSON fallback**: If SQLite is unavailable, shards results into `.json` files in `writeragent_grammar_cache.d/`.
- **Pruning**: Once per session, if the persistent cache exceeds 5000 entries, it prunes back to 4000 using LRU/mtime.
- **Normalization**: Uses `_normalize_for_sentence_cache` so trailing whitespace and redundant punctuation share keys. Errors are clipped to the canonical length.
- **Incomplete-prefix compaction**: On **`cache_put_sentence`**, when the normalized text is still **incomplete**, the cache walks the sentence `OrderedDict` newest-first (bounded scan per locale) and evicts strict-prefix incomplete predecessors so incremental typing does not fill the LRU with `"The"`, `"The qu"`, … stubs. Details and regression tests: [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py), [`test_sentence_cache_incomplete_prefix_compaction`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py). For the historical cross-sentence queue dedup bug, see [Appendix A](#appendix-a-cross-sentence-prefix-dedup).
- **Memory warm-up**: `cache_get_sentence` promotes persistence hits to the memory LRU cache via `_populate_memory_cache_only`. This ensures subsequent re-traversals of the same sentence are handled in memory without repeated disk I/O.
- **UI responsiveness**: Persistence writes in `cache_put_sentence` are performed outside the global `_CACHE_LOCK`, ensuring slow disk I/O does not block the foreground proofreading pass.
- **Optional document-embedded persistence (code)**: In [`grammar_persistence.py`](../plugin/writer/locale/grammar_persistence.py), set **`USE_SQLITE_CACHE = False`** to use **`DocumentPersistence`**: in-memory map per document id, load/save JSON under user-defined property **`WriterAgentGrammarCache`**, save on **`OnPrepareSave`** / **`OnSave`** / **`OnSaveAs`** / **`OnSaveTo`**, registry cleanup on **`OnUnload`** / dispose. [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) then skips the global LRU for sentence get/put when this mode is active and requires **`doc_id`** on those paths. **Default: `True`** (global SQLite / profile JSON unchanged).

> [!NOTE]
> **Alternative: Document-embedded Cache (optional implementation)**
> An optional persistence strategy (off by default) stores grammar sentence results **inside** the `.odt` (user-defined property) instead of the global profile database. Implementation: [`grammar_persistence.py`](../plugin/writer/locale/grammar_persistence.py) (`USE_SQLITE_CACHE`, `DocumentPersistence`, `get_persistence(ctx, doc_id)`).
> 
> - **Mechanism**: 
>   - **Load**: On document open (or first grammar call), load the cached errors from a hidden document property or a custom stream within the ODF package.
>   - **Run**: Use an in-memory cache for the duration of the session.
>   - **Save**: Hook into the `onPrepareSave` event to serialize the current cache back into the document.
> - **Storage Optimization (Avoiding Bloat)**:
>   - **Full Data for Errors**: Store the full error payload (wrong/correct text, suggestions, reasons) only for sentences that have issues.
>   - **Hashes for Clean Sentences**: For sentences with zero errors, store only their **fingerprint hash**. This allows the engine to recognize "I've checked this and it's perfect" with minimal storage overhead (e.g., 8-16 bytes per clean sentence).
> - **Pruning Strategies**:
>   - **Session Survival (Efficient)**: Only persist entries (both error payloads and clean hashes) that were actually requested during the current session. This naturally prunes deleted text with $O(1)$ overhead on save. *Note*: The risk of "prematurely pruning unread sections" is mitigated if the host application (LibreOffice) traverses the entire document on open/check.
> - **Cache vs. Official List**:
>   - **If it's just a cache**: (A performance optimization to avoid LLM costs), SQLite is superior because it shares knowledge across *all* documents.
>   - **If it's an "Official List"**: (A record of "the AI has reviewed this and these are the results"), then it *belongs* in the document. This turns the cache into a persistent "Grammar State" that ensures the document looks the same on every machine.
> - **Advantages**:
>   - **No Global Limit**: We don't have to worry about a global 5000-entry limit; each file manages its own cache size.
>   - **Portability**: If you send the document to a colleague (who also has WriterAgent), they see the squiggles immediately without re-triggering LLM calls.
>   - **Isolation**: Work on "Document A" doesn't evict cache entries for "Document B".
> - **Implementation Note**: The current architecture already uses **fingerprint hashes** (`fingerprint_for_text`) as the primary key for all sentence cache lookups (memory and SQLite). Adding a document-embedded layer would require zero refactoring of the core logic; it would simply be another persistence backend that stores and retrieves these existing hashes.
> - **Runtime ownership (implemented shape; why not `setattr` on the UNO document)**: In-process state is a Python `DocumentPersistence` per open document in a **module-level map** keyed by document id, with **`OnUnload`** / dispose handling to **unregister listeners** and **remove that map entry** so the object (and its in-memory cache) becomes unreachable and is garbage-collected. We **do not** hang that state on the PyUNO `XModel` wrapper via Python **`setattr`** as the primary registry: that would add **complexity and risk** (wrapper identity across threads and call paths, reference cycles with the model, and harder unit testing) for little benefit once explicit unload teardown is in place.
> - **Personal Opinion**: 
>   If we treat this purely as a **cache**, SQLite is better because it's more efficient for the user's total workflow (boilerplate text is checked once for all files). But if we want WriterAgent to provide a **consistent experience across collaborators**, document-embedding is the only way to ensure two people see the same underlines without double-spending tokens. 
>   **The Trade-off**: The primary risk is **privacy and bloat**. If a user checks a sensitive document and then shares the `.odt`, the cached text snippets might be recoverable from the file metadata even if the visible text is changed.


#### LLM wire format and parser

- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/framework/client/llm_client.py) with `response_format={"type":"json_object"}` on the OpenAI-compatible path (Together, OpenRouter, etc.; see docstring on `make_chat_request`), a system prompt (**`GRAMMAR_SYSTEM_PROMPT_TEMPLATE`** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py)) requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}` (schema description in English) plus the **document language** (BCP-47 and English name from the registry), and user message the **checked sentence text** for that worker item (one sentence per request in normal prose). The prompt explicitly asks for errors in the order they appear. For threshold-allowed partial slices, the prompt adds a conservative note that input may be partial. Parser: [`parse_grammar_json`](../plugin/writer/locale/grammar_proofread_locale.py) uses `safe_json_loads` then `json_repair` (with logging) when needed.

#### Offsets, whitespace, and markup

- **Offset normalization**: `normalize_errors_for_text` uses **`search_pos` tracking** to handle multiple occurrences of the same erroneous text within a window. If ordered scan fails and a global `find` matches **before** `search_pos`, that item is **skipped** (avoids anchoring duplicate substrings to the wrong occurrence).
- **Traversal whitespace**: `_apply_proofreading_end_positions` and initial empty-result advancement use Unicode **`str.isspace()`**, not ASCII space only, so tabs/NBSP between sentences advance Writer's next position correctly.
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### Why `enqueue_seq` exists (queue FIFO is not enough)

**Terminology.** The shipped code uses a **global integer counter** (`next_enqueue_seq()` / `_ENQUEUE_SEQ` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)), incremented when a cache miss enqueues work; each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) stores it as **`enqueue_seq`**. This is **not** the same as `time.monotonic()` — that clock is used elsewhere only for **elapsed milliseconds** on LLM requests (status/diagnostics), not for ordering queue items.

**Why not rely only on "everything goes through `queue.Queue`"?** A FIFO queue orders **`get()` dequeue order** among objects that are actually retrieved in sequence. The grammar worker deliberately does **more** than strict FIFO:

1. **Tail replace-in-place** — For the same `inflight_key`, a newer item can **overwrite** the last slot of the internal deque without establishing a simple FIFO relationship to items already consumed in an **earlier** batch. Queue position alone does not record "this snapshot superseded that one" across batches.

2. **Batch drain + `deduplicate_grammar_batch`** — The worker collects multiple `get()` results into one batch, then for each **`inflight_key`** keeps only the highest **`enqueue_seq`**.

3. **`_latest_seq` / pre-execute stale skip** — Before calling the LLM, the worker asks whether a **newer** enqueue has already been recorded for that `inflight_key`. **Post-LLM**: re-check before `cache_put_sentence`; if superseded during the HTTP call, skip the cache write.

So **`enqueue_seq` is a generation stamp for supersede/dedup semantics**, not a substitute for the queue. Something must play that role whenever work is merged, replaced, or skipped outside pure FIFO.

**Alternatives (same role, different representation):**

| Approach | Notes |
|----------|-------|
| **Per-`inflight_key` counter** | Bump only when enqueueing for that document+locale key. Same semantics as today's global counter for same-key comparisons; avoids mixing sequence space across unrelated documents (clearer for logs and reasoning). |
| **Enqueue-time monotonic value** | e.g. `time.monotonic()` at enqueue as the order key. Requires discipline if two enqueues share an identical timestamp resolution; still needs to be stored on each `GrammarWorkItem` and mirrored (like `_latest_seq`) for stale checks. |
| **Post-LLM staleness guard** | **Shipped:** `inflight_superseded(inflight_key, enqueue_seq)` after `chat_completion_sync` returns and before `cache_put_sentence`. |

### Tests

- Unit: [`plugin/tests/writer/locale/test_grammar_proofread_engine.py`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py) — JSON parsing, offset normalization, sentence cache roundtrip, trailing whitespace cache normalization, ignore rules, overlap expansion.
- Unit (work queue dedup): [`plugin/tests/writer/locale/test_grammar_work_queue.py`](../plugin/tests/writer/locale/test_grammar_work_queue.py) — same-key supersede, reverse-prefix chain reproducer, distinct `inflight_key` survival, **paragraph batching success, and LLM result-mismatch fallback**.
- Unit (queue / worker): [`plugin/tests/writer/locale/test_ai_grammar_proofreader_worker.py`](../plugin/tests/writer/locale/test_ai_grammar_proofreader_worker.py) — `GrammarWorkQueue` stale detection, legacy Lightproof finalize regression helper, pinned `proofread_sentence_text` worker path.
- UNO (native runner): [`plugin/tests/writer/locale/test_grammar_uno.py`](../plugin/tests/writer/locale/test_grammar_uno.py) — cache path, `ignoreRule`, incremental overlap (relocated paths; run via `plugin.testing_runner`).

### Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on the **Doc** tab; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | `doProofreading` does **not** wait on the main thread for LLM results (avoids dead menus while grammar runs). HTTP/LLM runs on a background worker; underlines update on a **later** proofreading pass when the sentence cache is ready. |
| Stale underlines | Sentence cache (locale + sentence text fingerprint) plus sequential work queue with same-key supersede, pre-execute stale skip, and post-LLM cache-write guard. **Cache hit** → immediate errors; **miss** → empty return once, queue worker fills cache for the next pass. See **Open backlog** for evolving this. |
| Concurrent chat agent | Optional guard (`doc.grammar_proofreader_pause_during_agent`) can skip grammar worker calls while chat/agent sends are active; grammar and chat/agent LLM requests also share one in-process request lane to avoid overlap races. |

---

## Optional repository reference: `GrammarChecker.py`

The standalone [`GrammarChecker.py`](../GrammarChecker.py) (repo root) was used historically as a prompt/threading reference. It is **not** bundled as WriterAgent product code. The shipped proofreader does **not** call it.

---

## Completed milestones

Major items that were previously listed as future work or cleanup but are **implemented in tree**:

- **Native UNO proofreader + registry**: `WriterAgentAiGrammarProofreader`, [`LinguisticWriterAgentGrammar.xcu`](../extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu), manifest wiring.
- **Persistent sentence cache**: SQLite (`writeragent_grammar.db`) with JSON shard fallback; LRU pruning; normalization and ignore rules.
- **Paragraph/sentence LLM batching**: Configurable chunk size, batch prompt template, per-chunk fallback when result counts mismatch.
- **Incomplete-prefix compaction** in the sentence LRU (typing stubs).
- **Same-key-only queue dedup**: `deduplicate_grammar_batch` keeps highest `enqueue_seq` per `inflight_key`; cross-key string-prefix dedup removed (see [Appendix A](#appendix-a-cross-sentence-prefix-dedup)).
- **Regex safety**: `_sterm_class` built with `re.escape` over sentence-terminator characters (`grammar_proofread_locale.py`).
- **Whitespace / hot regex**: `GRAMMAR_WHITESPACE_RUN_RE` and related patterns precompiled at module load where appropriate.
- **Persistence initialization**: thread-safe singleton setup for grammar cache persistence (`grammar_persistence.py`); no unsafe fork-based locking.
- **Worker idle batching**: quiet period via `GRAMMAR_WORKER_PAUSE_TIMEOUT_S` on queue `get`, coalescing bursts before LLM calls.
- **Optional grammar-only model**: `doc.grammar_proofreader_model` (Doc tab); empty uses the chat text model so grammar can be pointed at a cheaper or local endpoint without changing chat defaults.

---

## Open backlog

Two tables: **product / hardening** (user-visible or systemic improvements) and **code health** (maintainability). Status is **open** unless noted.

### Product and hardening

| ID | Task | Notes |
|----|------|--------|
| P1 | Native linguistic integration | Research built-in `SpellChecker` / morphological analysis before or alongside LLM (Lightproof-style). |
| P2 | HTTP 429 / backoff | Theoretical: Exponential backoff and cooldown in the grammar worker if providers ever rate-limit; currently unnecessary due to `LlmClient` request pacing. |
| P3 | Locales | Optional regional tags in XCU if an LO build needs explicit `hasLocale`/`getLocales` pairing beyond normalization. |
| P4 | Refresh UX | LO shows new squiggles on subsequent passes — document for users; research safe invalidate APIs if any. |
| P5 | Optional model / temperature | Surface more controls in Settings if needed (grammar model override exists). |
| P6 | Document-generation invalidation | Fold revision/mod-generation into cache keys if LO exposes it; reduces stale offsets after edits above span. |
| P7 | Shared policy with chat | Expand beyond pause-during-agent + shared LLM lane (endpoint-aware policy, status UX, adaptive queue). |
| P8 | Prompt and schema hardening | Few-shot edge cases (quotes, lists, track changes); stricter JSON recovery. |
| P9 | Paragraph / traversal tuning | Compare sentence selection vs stock Lightproof (`len(rText)` etc.) if underlines misbehave on some LO versions. |
| P10 | Ignore rules | Persist `ignoreRule` across sessions; locale-specific ignores if API evolves. |
| P11 | Observability | Cache hit rate, supersede counts, p50/p95 schedule→`cache_put` behind a verbose flag. |
| P12 | Accessibility / UX copy | Clear copy that grammar is asynchronous; link Writing aids when multiple proofreaders exist. |
| P13 | LanguageTool-class local checking | Research roadmap: [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md). |
| P14 | Parallel grammar worker | Optional limited parallelism across **distinct** documents while respecting `llm_request_lane`. |
| P15 | Queue priority / visibility | Prefer currently edited or visible ranges over scroll-induced backlog (related to **C5**). |
| P16 | Remove obsolete timeout config | Clean up `doc.grammar_proofreader_wait_timeout_ms` from `plugin/doc/module.yaml` and UI — the proofreader return path no longer uses it, and it clutters settings. |
| P17 | Configurable LLM max tokens | Expose the hardcoded **3072** max output tokens as `doc.grammar_proofreader_max_tokens` so users can tune for different endpoints or models. |
| P18 | Configurable max chars | Move `GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS` (8192) to a config key `doc.grammar_proofreader_max_chars`; allows tuning for very long sentences without code changes. |
| P19 | Batch size validation | Enforce `1 <= doc.grammar_proofreader_batch_sentences <= 8` at config read time; log **WARNING** if out of range and clamp to bounds. |
| P20 | Document-embedded cache | Optional: `USE_SQLITE_CACHE = False` in [`grammar_persistence.py`](../plugin/writer/locale/grammar_persistence.py) + user-defined property `WriterAgentGrammarCache`; see [Alternative](#alternative-document-embedded-cache-proposed) and Sentence cache bullets. |
| P23 | Regional locale opt-out | Allow specific locales (e.g., `en-AU`, `pt-PT`) to opt-out of normalization to the "base" language if regional grammar nuances are significant. |

### Code health and maintainability

| ID | Task | Notes |
|----|------|--------|
| C1 | Tiered error handling in `doProofreading` | Reduce nested try/except that only log-and-continue; extract `_safe_*` helpers so failures are visible in tests. |
| C2 | Optional `unohelper` consolidation | Top-level import serves `unohelper.Base`; registration block imports again for `ImplementationHelper` — optional single pattern for clarity. |
| C3 | HTTP 429 / backoff | Same work as **P2** (worker / `run_llm_and_cache_batch`). |
| C5 | Viewport / LIFO-ish priority | Mitigate scroll enqueue starving active typing (**P15**). |
| C6 | Regex audit | Most patterns are compiled; audit [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py) for any remaining compile-per-call hot paths. |
| C7 | Logging discipline | Structured events, avoid duplicate levels, DEBUG vs INFO boundaries ([Appendix B](#appendix-b-structural-notes)). |
| C8 | ProofreadingResult helpers / hints | Optional `@dataclass`-style helpers or richer type hints for UNO structs where stubs help. |
| C10 | Batch diagnostics logging | Add structured **DEBUG** logs for batch stats: `sentences_queued`, `sentences_deduped`, `sentences_stale_skipped`, `sentences_llm_requested`, `llm_request_duration_ms` to help diagnose performance and correctness issues. |
| C11 | Module docstrings | Add `"""Real-time grammar proofreading via UNO XProofreader + LLM."""` docstrings to `ai_grammar_proofreader.py`, `grammar_work_queue.py`, and `grammar_proofread_cache.py` for better IDE support and maintainability. |
| C12 | Constants documentation | Document all `GRAMMAR_*` constants in `grammar_proofread_locale.py` with **units**, **default values**, and **rationale** (e.g., why 8192 chars, why 2048 tokens) as inline comments or a module-level docstring section. |
| C13 | Remove dead code | Delete any remaining references to `doc.grammar_proofreader_wait_timeout_ms` (config reads, UI bindings, validation) that are now defunct. |
| C15 | Update normalization comment | Correct the stale comment in `_normalize_for_sentence_cache` (grammar_proofread_cache.py) which claims it uses a subset of terminators. |

### Tests

| ID | Task | Notes |
|----|------|-------|
| T1 | HTTP 429 backoff tests | Add unit tests `test_429_backoff_retry_succeeds` (retry after delay, succeeds) and `test_429_exhausted_returns_empty` (max retries exhausted, returns empty errors) in `test_grammar_work_queue.py`. |
| T2 | Batch mismatch edge cases | Add tests for LLM returning **fewer results** than sentences, **malformed JSON** in batch response, and **empty batch chunk** — verify fallback to individual processing and graceful degradation in `run_llm_and_cache_batch`. |
| T3 | Cache pruning and promotion | Add `test_persistent_cache_pruning_5000_to_4000` (verify pruning triggers at 5000 and reduces to 4000) and `test_memory_cache_promotion_on_persistence_hit` (verify SQLite/JSON hits populate the in-memory LRU) in `test_grammar_proofread_cache.py`. |
| T4 | Locale normalization roundtrips | Add tests in `test_grammar_proofread_locale.py` verifying `en_US`→`en-US`, `fr_FR`→`fr-FR`, `de_DE`→`de-DE`, `zh_CN`→`zh-CN` normalization, and confirm unsupported locales return empty `getLocales()` list. |
| T5 | Stale sequence race condition | Add test `test_stale_sequence_race_skips_superseded` in `test_ai_grammar_proofreader_worker.py`: enqueue item A, then B (same `inflight_key`, `enqueue_seq+1`), ensure A is skipped during drain and only B is processed/cached. |
| T6 | Duplicate substring guard | Add regression test `test_duplicate_substring_normalization` in `test_grammar_proofread_locale.py`: verify `normalize_errors_for_text` correctly anchors errors when the same substring (e.g., `"the the"`) appears multiple times in a sentence. |
| T7 | Whitespace normalization | Add test `test_strip_zero_width_chars` in `test_grammar_proofread_text.py`: verify `split_into_sentences` strips carriage return (CR), form feed (FF), vertical tab (VT), NUL, and normalizes tab to space. |
| T8 | Trailing punctuation normalization | Add test `test_trailing_punct_compaction` in `test_grammar_proofread_cache.py`: verify `_normalize_for_sentence_cache` maps `"Hello!?"` → `"Hello!"`, `"Hello?.."` → `"Hello?"`, and `"Test..."` → `"Test."` for cache key sharing. |

---

## Appendices

### Appendix A: Cross-sentence prefix dedup

**Problem:** An older implementation added a *second* dedup step that grouped queue items by `(doc_id, locale)` and dropped items whose **slice text** was in a **string prefix** relation with another item (newest `enqueue_seq` wins). That matches typing inside **one** sentence, but `inflight_key` is already scoped per sentence. **Different sentences** in the same paragraph can still have texts where one is a prefix of the other (e.g. first sentence `No.` and a later sentence `No problem today.`). Cross-key prefix logic **dropped the shorter sentence's work** and skipped a valid LLM check.

**Fix shipped:** `deduplicate_grammar_batch` only keeps, for each **`inflight_key`**, the item with the highest **`enqueue_seq`**. No text-prefix pass across distinct keys. Same-sentence typing is covered by the same `inflight_key` plus enqueue tail-replace.

**Other approaches** (if redesigning — avoid regressions):

| Approach | Notes |
|----------|-------|
| Prefix-newest-wins **only for the same `inflight_key`** | Narrow the old idea to the typing timeline only; often equivalent to one survivor per key after the main dedup. |
| **Span-aware** prefix rules | Drop prefix-related items only when `n_start`/`n_end` ranges overlap (same physical sentence), not when offsets differ. |
| **No cross-key text comparison** | Rely on `inflight_key` + tail-replace only (**current**). |

**Regression test:** [`test_two_sentences_string_prefix_collision_both_survive`](../plugin/tests/writer/locale/test_grammar_work_queue.py). Implementation notes are in **comments directly above** `deduplicate_grammar_batch` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py).

### Appendix B: Structural notes

Ideas from earlier cleanup planning — not scheduled as separate tickets:

**Error handling tiers**

| Level | Action | Example |
|-------|--------|---------|
| **Fatal** | Raise / return None | UNO module missing, `createUnoStruct` fails |
| **Recoverable** | Log ERROR + return empty/default | Config read fails, locale not supported |
| **Diagnostic** | Log INFO/DEBUG + continue | Cache miss, queue deduplication |

**Testing**

- Prefer injectable helpers over patching `time.sleep` at module scope where sleeps exist.
- Prefer pure functions for dedup/stale logic (already partly true).

### Appendix C: Documentation maintenance

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).
- Optional non-LLM checker roadmap: [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md).
