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
| **Appendices** | Historical bug write-ups, [dialogue / BreakIterator split limitation](#appendix-e-dialogue-splits), doc-maintenance pointers |

### At a glance

- **Native grammar** is implemented as an `XProofreader` service with Lightproof-style registry (`LinguisticWriterAgentGrammar.xcu`); users enable LLM work on the **Doc** tab and pick the proofreader under Writing aids.
- **Batching** groups sentences from the same paragraph into chunked LLM requests; batch size is capped (`doc.grammar_proofreader_batch_sentences`, max 8).
- **Language Detection** automatically identifies mismatches between LibreOffice's `CharLocale` and the actual text using a lightweight LLM call, actively triggering localized paragraph re-checks (`doc.grammar_proofreader_detect_language`).
- **Cache** is **document-embedded**: results live inside the `.odt` as user-defined property `WriterAgentGrammarCache`, so they travel with the file across machines and collaborators. The global SQLite cache was removed since we save directly with the document.
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
- **Pure Python modules**: [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py) — **`GRAMMAR_REGISTRY_LOCALE_TAGS`**, UNO `Locale` ↔ BCP-47 bridging; Unicode sentence terminals, `looks_complete_sentence`, abbrev table, system prompt templates (**single and batch**), `parse_grammar_json`, `parse_grammar_batch_json`. [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py) — BreakIterator orchestration, `split_into_sentences`, offset normalization. [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) — sentence LRU + ignore rules. [`grammar_obs.py`](../plugin/writer/locale/grammar_obs.py) — DEBUG `grammar_obs`, sidebar `emit_grammar_status`, `slice_preview_debug`. [`grammar_worker_llm.py`](../plugin/writer/locale/grammar_worker_llm.py) — sync grammar + language-detect LLM calls, prompt build, JSON parse. [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py) — `GrammarWorkItem`, `GrammarWorkQueue`, `run_llm_and_cache_batch` (chunk orchestration).
- **Linear worker phases**: `run_llm_and_cache_batch` delegates to `_worker_*` helpers, then optional `_run_language_validation` and `_run_grammar_check`. Pure branch rules live in [`grammar_worker_phases.py`](../plugin/writer/locale/grammar_worker_phases.py); sync LLM wire lives in [`grammar_worker_llm.py`](../plugin/writer/locale/grammar_worker_llm.py).
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

- **(Historical) Enqueue-time tail-replace (Layer 1)**: Removed in the TD4 simplification follow-up. The old O(1) in-place replace under `self._q.mutex` + direct deque mutation was ineffective during real typing bursts (the worker drains so quickly the queue is usually empty on the next enqueue). All same-key collapse now happens in the Layer 2 drain dict + canonical `deduplicate_grammar_batch` + Layer 3 `_latest_seq` guards. The mental model and rationale are in the module docstring of `grammar_work_queue.py`.
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

<a id="dialogue-breakiterator-limitation"></a>

#### Dialogue and quoted speech (BreakIterator limitation)

**Example:** `"Fire! Fire!"` — LibreOffice `BreakIterator` typically ends the first “sentence” after the first `!`, so the checker may send **`"Fire!`** (opening quote, no closing quote yet) to the LLM as a standalone unit. The abbreviation extension logic only revises boundaries when the candidate end falls on **`.`** and `word_before_period_is_abbrev` applies; **`!` and `?` inside speech are not extended.** The fragment still ends in `!`, so `looks_complete_sentence` is true and the slice is treated as **complete**, not as a short partial to drop.

**User-visible effect:** The model often reports a **missing closing quotation mark** (or similar dialogue punctuation). That is a sensible reading of the **isolated substring**, not a random hallucination.

**Why a naive “merge until quotes balance” is dangerous:** If the implementation simply walks forward and **concatenates every following BreakIterator segment until the closing `"` appears**, a long stretch of dialogue can turn into **one enormous pseudo-sentence** spanning **many** underlying LO sentence boundaries. That undermines **sentence-level caching** (one huge key, any edit inside the quote invalidates it), **batching** (`GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS`, worker latency), and the product goal of **localized squiggles**. A robust design needs **hard caps** (max extra characters and/or max number of merged segments), a defined **fallback** when the cap is hit (e.g. keep the first segment only, or stop merging and accept occasional false positives), and **UNO tests** with real `BreakIterator` plus unit tests for merge logic. Narrower triggers (e.g. only consider extension when the split is on `!`/`?` and quote parity is odd) reduce collateral damage in ordinary prose. See [Appendix E](#appendix-e-dialogue-splits) for a structured write-up.

#### Sentence cache

- **In-memory LRU**: Keyed by sentence fingerprint (locale + text hash). `MAX_CACHE_SIZE` is **2048**.
- **Persistent storage**: We removed the global SQLite and JSON sharding cache backends because we now save grammar check results directly inside the document.
- **Normalization**: Uses `_normalize_for_sentence_cache` so trailing whitespace and redundant punctuation share keys. Errors are clipped to the canonical length.
- **Incomplete-prefix compaction**: On **`cache_put_sentence`**, when the normalized text is still **incomplete**, the cache walks the sentence `OrderedDict` newest-first (bounded scan per locale) and evicts strict-prefix incomplete predecessors so incremental typing does not fill the LRU with `"The"`, `"The qu"`, … stubs. Details and regression tests: [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py), [`test_sentence_cache_incomplete_prefix_compaction`](../plugin/tests/writer/locale/test_grammar_proofread_engine.py). For the historical cross-sentence queue dedup bug, see [Appendix A](#appendix-a-cross-sentence-prefix-dedup).
- **Memory warm-up**: `cache_get_sentence` promotes persistence hits to the memory LRU cache via `_populate_memory_cache_only`. This ensures subsequent re-traversals of the same sentence are handled in memory without repeated disk I/O.
- **UI responsiveness**: Persistence writes in `cache_put_sentence` are performed outside the global `_CACHE_LOCK`, ensuring slow disk I/O does not block the foreground proofreading pass.
- **Document-embedded persistence**: [`DocumentPersistence`](../plugin/writer/locale/grammar_persistence.py) keeps an in-memory map per document id, loads from user-defined property **`WriterAgentGrammarCache`** on first grammar call, and writes JSON back on **`OnPrepareSave`** / **`OnSave`** / **`OnSaveAs`** / **`OnSaveTo`** via `set_document_property` in [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py). A single **`XDocumentEventListener`** handles document events and broadcaster `disposing` (registered on `addDocumentEventListener` and `addEventListener`). Registry cleanup runs on `OnUnload` / dispose, removing the per-document entry from the module-level map so the wrapper and its memory cache become unreachable. [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py) manages this mode actively using **`doc_id`** on `cache_get_sentence` / `cache_put_sentence`.

> [!NOTE]
> <a id="document-embedded-cache-default"></a>
> **Document-embedded cache — design notes (shipped default)**
>
> Grammar results travel with the `.odt` as a user-defined string property (`WriterAgentGrammarCache`). The global SQLite/profile-JSON cache was removed as we now save directly with the document.
>
> **Why default to embedded:**
>
> - **Stays with the document.** Open the same `.odt` on a new machine (or hand it to a collaborator with WriterAgent) and the squiggles appear immediately without re-triggering LLM calls.
> - **No global cache ceiling.** Each file manages its own size — no shared 5000-entry pruning, no `writeragent_grammar.db` growing across unrelated projects.
> - **Isolation.** Work on document A never evicts entries for document B.
> - **Treats reviewed grammar as document state**, not as a hidden machine-local optimization: the AI's verdict on each sentence is recorded next to the prose it judged.
>
> **Trade-offs (vs global disk cache):**
>
> - **No cross-document reuse.** Boilerplate phrases that repeat across files are re-checked the first time per file (still cached within each file).
> - **Privacy / file-share footprint.** The user-property contains sentence fingerprints and full LLM error payloads (suggestions, comments). If the document is shared, this metadata travels with it. Hashes are SHA-256 of normalized sentences, so the raw sentence text is not directly recoverable from the cache itself, but error payloads quote the wrong fragments by string. Users sharing a sensitive draft can clear the property by stripping user-defined properties before sending.
> - **Save-cycle pruning is by `_session_accessed`.** Only sentences the proofreader actually looked at during the current session are persisted on save. Writer's open-time proofreading pass touches visible paragraphs, but sections that were never scrolled into view can disappear from the cache after a save. (See **Size and pruning** below — listed in the backlog as P22 "full-doc retention on save".)
>
> **On-disk format (`WriterAgentGrammarCache`):**
>
> ```jsonc
> {
>   "<sha256-hex fingerprint of normalized sentence>": [
>     {
>       "n_error_start": 4,
>       "n_error_length": 3,
>       "suggestions": ["..."],
>       "short_comment": "...",
>       "full_comment": "...",
>       "rule_identifier": "..."
>     }
>   ],
>   "<another fingerprint>": []   // clean sentence (reviewed, no errors)
> }
> ```
>
> Clean sentences are stored as the fingerprint mapped to an empty array — they're how the engine remembers "I've already checked this one and there's nothing to flag," and they're the bulk of a typical document's payload. The serialized JSON is currently capped at **900 KB**; over that, the save is skipped with a warning.
>
> **Size budget today.** Each fingerprint is a full SHA-256 hex (64 chars). A clean entry is ~70 bytes; a sentence with one short error is ~250–350 bytes. A 5000-sentence all-clean document fits comfortably (~350 KB); long novels with many errors approach the cap.
>
> **Size-optimization options (deferred, P21 in the backlog):**
>
> | Lever | Approx. savings | Notes |
> |-------|-----------------|-------|
> | Truncate fingerprint to 16-hex (64-bit) or 22-char URL-safe base64 (128-bit) | -50 to -75% on keys | 64-bit hash collision probability ≈ 10⁻¹² at 5000 sentences; needs a payload-version bump (`{"v":2,...}`) and migration of any in-the-wild caches. Cache-only data, so old caches can simply be discarded. |
> | Shorten error-dict keys (`n_error_start` → `s`, `n_error_length` → `l`, `suggestions` → `g`, `short_comment` → `c`, `full_comment` → `f`, `rule_identifier` → `r`) | -30 to -50 bytes per error | Mechanical; same version bump covers it. |
> | Split clean-set from dirty-map: `{"v":2, "clean":"hash1hash2…", "dirty":{...}}` | Removes `[]` + comma + quoted-key overhead on the (typical majority) clean sentences | Concatenating fixed-width fingerprints is significantly cheaper than per-key JSON entries. |
> | Drop `short_comment` if always derivable from `full_comment` | Modest | Audit usage first — Writer UI distinguishes them in the proofreading dialog. |
> | gzip + base64 the whole payload into the property | Often 3–4× | Adds CPU per save/load and makes the saved property opaque to other tools that might want to inspect it. Reasonable last resort once smaller fixes are in. |
>
> The first three are nearly free wins and additive; the last two are only worth doing if real-world documents start hitting the 900 KB cap.
>
> **Decision (2025):**
> - **Fingerprint size: 96-bit (24 hex characters)**. Collision probability at 5,000 sentences is ~1 in 10²¹ — effectively **never** in practice. This is the chosen balance: excellent safety margin with **62.5% size reduction** from 64-bit. 80-bit (20 chars) offers ~68.75% savings with ~1 in 10¹⁶ collision probability, which is still astronomically unlikely but was rejected because a hash mismatch could cause a sentence to incorrectly show or hide errors. 96-bit pushes the risk to "practically never" for any realistic document size.
> - **Split clean/dirty storage: yes**. Clean sentences (the majority) are stored as a single concatenated string of fingerprints under a `"good"` or `"clean"` key, eliminating per-key JSON overhead. For a 5,000-sentence all-clean document, this reduces the payload from ~355 KB to ~120 KB (24 chars × 5000 = 120,000 bytes for the concatenated fingerprints, vs ~70 bytes × 5000 = ~350,000 bytes for the original per-key format plus commas). That's a **~66% reduction** for the clean set alone. Dirty sentences remain in a map under a `"bad"` or `"dirty"` key with compact error-dict keys.
> - **Compact error keys: yes**. Use `s`, `l`, `g`, `c`, `f`, `r` for the six error fields, saving ~43 bytes per error.
>
> **v2 payload example:**
> ```json
> {
>   "v": 2,
>   "good": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8...",
>   "bad": {
>     "a1b2c3d4e5f6a7b8c9d0e1": [{"s":4,"l":3,"g":["fix"],"c":"Err","f":"Detail","r":"wa_1"}]
>   }
> }
> ```
>
> **Implementation reuse.** The architecture already keyed all sentence cache lookups on `fingerprint_for_text`, so adding `DocumentPersistence` didn't require core refactoring — it's just another `GrammarPersistence` backend.
>
> **Why not `setattr` on the UNO model wrapper?** State is held in a module-level `_doc_persistence_instances: dict[doc_id, DocumentPersistence]`, with `OnUnload` / dispose dropping the entry so the object (and its in-memory cache) becomes unreachable. Hanging it on the PyUNO `XModel` wrapper via Python `setattr` is rejected: wrapper identity is not stable across threads/call paths, it creates ref cycles with the model, and it's harder to unit-test.


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
- **Persistent sentence cache**: Embedded directly within document properties; LRU memory pruning; normalization and ignore rules.
- **Paragraph/sentence LLM batching**: Configurable chunk size, batch prompt template, per-chunk fallback when result counts mismatch.
- **Incomplete-prefix compaction** in the sentence LRU (typing stubs).
- **Same-key-only queue dedup**: `deduplicate_grammar_batch` keeps highest `enqueue_seq` per `inflight_key`; cross-key string-prefix dedup removed (see [Appendix A](#appendix-a-cross-sentence-prefix-dedup)).
- **Regex safety**: `_sterm_class` built with `re.escape` over sentence-terminator characters (`grammar_proofread_locale.py`).
- **Whitespace / hot regex**: `GRAMMAR_WHITESPACE_RUN_RE` and related patterns precompiled at module load where appropriate.
- **Persistence initialization**: thread-safe singleton setup for grammar cache persistence (`grammar_persistence.py`); no unsafe fork-based locking.
- **Worker idle batching**: quiet period via `GRAMMAR_WORKER_PAUSE_TIMEOUT_S` on queue `get`, coalescing bursts before LLM calls.
- **Optional grammar-only model**: `doc.grammar_proofreader_model` (Doc tab); empty uses the chat text model so grammar can be pointed at a cheaper or local endpoint without changing chat defaults.
- **No plaintext in persistence**: Cache lookup has always been by fingerprint and only `errors` are read/written, completely avoiding storing plaintext sentence text in any persistent file/property.
- **Document-embedded persistence**: [`DocumentPersistence`](../plugin/writer/locale/grammar_persistence.py) is the sole shipped persistence mechanism. `OnPrepareSave` / `OnSave` / `OnSaveAs` / `OnSaveTo` write `WriterAgentGrammarCache` back to the doc; `OnUnload` / broadcaster `disposing` clean up per-document state. The UNO listener uses the correct `documentEventOccured` callback (the earlier `documentEvent` typo silently dropped every save), and `set_document_property` detects existing properties via `XPropertySet.getPropertySetInfo().hasPropertyByName` (the `UserDefinedProperties` `PropertyBag` does **not** implement `XNameAccess`) — without that check the second save raised `Property name or handle already used` and the JSON never landed in the file. See [Appendix D](#appendix-d-documentpersistence-save-fix) for the historical write-up.
- **Persistent Ignore Rules (Native Integration)**: Hooked into native `ignoreRule` and `resetIgnoreRules` within the `XProofreader` implementation. Ignored rules are saved/loaded per-document under the `WriterAgentGrammarCache` property of `DocumentProperties`. Implemented dynamic negative constraint prompt injection in `grammar_work_queue.py` and clean post-normalization filtering so ignored rules are thrown away immediately upon LLM completion and never pollute the persistent cache. Simplified the rule identifier schema to `wa_g_rule||{reason}` to allow instant string-slice decoding instead of split routines, and normalized reasons to preserve quote-enclosed specific text (e.g. keeping `'an'` and `'a'` inside `"use an instead of a"`). Added a full unit test suite covering the entire pipeline.
- **Remove obsolete timeout config (P16)**: Completely removed the obsolete configuration option `grammar_proofreader_wait_timeout_ms` from `plugin/doc/module.yaml`, cleaning up the settings UI and simplifying the configuration schema since the proofreader return path operates asynchronously.
- **Compact document-embedded payload (P21)**: Fully transitioned to the v2 payload serialization format, utilizing 24-character/96-bit fingerprints, splitting clean and dirty sentence cache elements into distinct `"good"` (list) and `"bad"` (dictionary) schemas, and employing compact single-character keys (`s`/`l`/`g`/`c`/`f`/`r`) for error records. This ensures high efficiency and drastically reduces payload sizes in user-defined document properties.

---

## Open backlog

Two tables: **product / hardening** (user-visible or systemic improvements) and **code health** (maintainability). Status is **open** unless noted.

### Product and hardening

| ID | Task | Notes |
|----|------|--------|
| P2 | HTTP 429 / backoff | Theoretical: Exponential backoff and cooldown in the grammar worker if providers ever rate-limit; currently unnecessary due to `LlmClient` request pacing. |
| P3 | Locales | Optional regional tags in XCU if an LO build needs explicit `hasLocale`/`getLocales` pairing beyond normalization. |
| P4 | Refresh UX | LO shows new squiggles on subsequent passes — document for users; research safe invalidate APIs if any. |
| P6 | Document-generation invalidation | Fold revision/mod-generation into cache keys if LO exposes it; reduces stale offsets after edits above span. |
| P7 | Shared policy with chat | Expand beyond pause-during-agent + shared LLM lane (endpoint-aware policy, status UX, adaptive queue). |
| P8 | Prompt and schema hardening | Few-shot edge cases (quotes, lists, track changes); stricter JSON recovery. |
| P11 | Observability | Cache hit rate, supersede counts, p50/p95 schedule→`cache_put` behind a verbose flag. |
| P13 | LanguageTool-class local checking | Research roadmap: [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md). |
| P14 | Parallel grammar worker | Optional limited parallelism across **distinct** documents while respecting `llm_request_lane`. |
| P15 | Queue priority / visibility | Prefer currently edited or visible ranges over scroll-induced backlog (related to **C5**). |
| P17 | Configurable LLM max tokens | Expose the hardcoded **3072** max output tokens as `doc.grammar_proofreader_max_tokens` so users can tune for different endpoints or models. |
| P18 | Configurable max chars | Move `GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS` (8192) to a config key `doc.grammar_proofreader_max_chars`; allows tuning for very long sentences without code changes. |
| P19 | Batch size validation | Enforce `1 <= doc.grammar_proofreader_batch_sentences <= 8` at config read time; log **WARNING** if out of range and clamp to bounds. |
| P22 | Embedded cache: full-document retention on save | Today `_persist_to_udprops` only writes sentences in `_session_accessed` (touched this session). Document sections that were never scrolled into view can drop after a save. Either persist `_memory_cache` in full when small enough, or always re-include previously persisted fingerprints we haven't explicitly invalidated. Trade-off vs cap and edit-detection cost. |
| P24 | Regional locale opt-out | Allow specific locales (e.g., `en-AU`, `pt-PT`) to opt-out of normalization to the "base" language if regional grammar nuances are significant. |
| P25 | Quote-aware sentence merge (optional) | Reduce false "missing quote" on dialogue split at `!`/`?` inside quotes. Requires capped post-`split_into_sentences` merge, i18n-safe quote rules, UNO + unit tests. See [Dialogue / BreakIterator limitation](#dialogue-breakiterator-limitation) and [Appendix E](#appendix-e-dialogue-splits). |
| P26 | Hybrid L1/L2 Cache (Cross-document sharing) | Implement a Tier 1 (Global Memory LRU) + Tier 2 (Document-embedded) hybrid. Allows copy-pasted sentences to hit the cache across different documents in the same session without global disk accumulation. See [Appendix G](#appendix-g-cross-document-memory-sharing). |

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
| T3 | Cache pruning and promotion | Add `test_memory_cache_promotion_on_persistence_hit` (verify document-persistence hits populate the in-memory LRU) in `test_grammar_proofread_cache.py`. |
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

### Appendix D: `DocumentPersistence` save fix

When document-embedded persistence was first wired up, no JSON ever landed in the saved `.odt`. The log on each Ctrl+S showed:

```
set_document_property error: Add property failed: Property name or handle already used.
[grammar] DocumentPersistence: save user property failed: Add property failed: Property name or handle already used.
```

Two latent bugs combined:

1. **Listener callback name typo.** `_GrammarDocumentEventListener` defined `documentEvent(self, Event)`. The UNO `XDocumentEventListener` IDL method is **`documentEventOccured`** (note the spelling). LibreOffice happily registered the listener but never dispatched any event to our handler, so `_persist_to_udprops` was simply never called. Fixed by renaming the method and consolidating broadcaster `disposing` into the same class (the interface inherits from `lang.XEventListener`).

2. **Wrong existence check in `set_document_property`.** The old code used `hasattr(props, "hasByName")` to decide between `addProperty` and `setPropertyValue`. But `UserDefinedProperties` is a `com.sun.star.beans.PropertyBag` that implements `XPropertyContainer` + `XPropertySet` — **not** `XNameAccess`. `hasByName` does not exist on it, so `exists` was always `False` and the code always took the `addProperty` branch. First save created the property fine; every save after that raised `Property name or handle already used`, the exception bubbled up, and `_persist_to_udprops` logged the warning above. The XModel's `WriterAgentGrammarCache` was never updated, so what LibreOffice wrote to disk was stale.

   Fixed by checking existence via **`XPropertySet.getPropertySetInfo().hasPropertyByName(name)`** (the actual `XPropertySet` API on `UserDefinedProperties`), with `hasByName` kept only as a secondary path for objects that happen to expose it. `get_document_property` was updated symmetrically, which also silences the `Get property value fallback failed: WriterAgentGrammarCache` warning on first open.

**Regression tests:**

- [`test_document_event_listener_has_correct_uno_method_names`](../plugin/tests/writer/locale/test_grammar_persistence.py) — pins the method name and signature.
- [`test_set_document_property_updates_existing_without_readding`](../plugin/tests/writer/test_document_helpers.py) — second save updates via `setPropertyValue`, no `addProperty` call.
- [`test_set_document_property_creates_missing_property`](../plugin/tests/writer/test_document_helpers.py) — first save still uses `addProperty`.
- [`test_get_document_property_returns_default_when_missing_without_warning`](../plugin/tests/writer/test_document_helpers.py) — first load returns default quietly.

### Appendix C: Documentation maintenance

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).
- Optional non-LLM checker roadmap: [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md).

<a id="appendix-e-dialogue-splits"></a>

### Appendix E: Dialogue splits and false closing-quote warnings

**Observed behavior:** Quoted lines where **sentence-ending punctuation appears before the closing quote** (e.g. `"Fire! Fire!"`, or multi-clause speech with internal `!` / `?`) can produce grammar suggestions about **missing closing quotation marks** or other dialogue punctuation errors.

**Root cause (implementation):**

1. **Primary split:** [`split_into_sentences`](../plugin/writer/locale/grammar_proofread_text.py) uses `com.sun.star.i18n.BreakIterator.endOfSentence`. For common English locales, **`!` and `?` end a sentence** the same way `.` does, including when they appear inside an opening `"` …
2. **No `!`/`?` analogue to the abbrev heuristic:** The loop that extends past a false `.` boundary only runs when the boundary character is **`.`** and the preceding token matches `word_before_period_is_abbrev`. There is **no** parallel path for “this `!` is mid-utterance inside dialogue.”
3. **Completeness gating still passes:** [`looks_complete_sentence`](../plugin/writer/locale/grammar_proofread_locale.py) treats the last non-closer character as the sentence terminal; `"Fire!` ends in `!`, so the chunk counts as **complete** and is not filtered as an incomplete short fragment.
4. **Pinned text to the LLM:** [`WriterAgentAiGrammarProofreader`](../plugin/writer/locale/ai_grammar_proofreader.py) enqueues [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) with `proofread_sentence_text` set to that segment. The worker does **not** re-split, so the model genuinely receives a **truncated quoted string**.

**Why this is a hardening problem, not “fix the prompt only”:** Prompt tweaks might reduce false positives but do not fix **wrong work units**, **cache granularity**, or **offset** semantics if we ever map errors back across merged spans.

**Robust mitigation ideas (design space):**

| Direction | Strength | Risk |
|-----------|----------|------|
| **Post-split merge with quote parity** | Aligns LLM input with author intent for many dialogue lines | Apostrophes, nested `"`/`'`, guillemets, RTL, and mixed curly/ASCII quotes need explicit policy; wrong parity can merge too much or too little. |
| **Hard cap on forward merge** | Prevents one open quote from absorbing **many** BreakIterator sentences until the closing quote—avoiding runaway **token cost**, **cache key bloat**, and **8192-char** pressure | Below the cap, some long speech blocks may still split incorrectly; above the cap, fall back must be defined. |
| **Trigger only on `!`/`?` boundaries** | Avoids touching the common `.` + abbrev path | Misses edge cases like period-inside-dialogue when BI still splits early. |
| **Prompt-only “ignore incomplete quote”** | Cheap | Leaves bad segmentation and weak cache behavior unchanged. |

**Regression surface:** Any change should add coverage in [`plugin/tests/writer/locale/test_grammar_proofread_text_uno.py`](../plugin/tests/writer/locale/test_grammar_proofread_text_uno.py) (real `BreakIterator`) and focused unit tests for merge helpers without UNO.

**Status:** Documented limitation + backlog **P25**; no code change required for users who hit this rarely.

### Appendix F: Real-time Language Detection Architecture

**Problem:** Users often type in multiple languages (e.g., mixing English and French sentences in the same document) without actively updating the LibreOffice language property. When the grammar checker runs, it applies rules for the wrong language. LibreOffice's built-in automatic language detection is often flaky or relies on rigid dictionaries.

**Implementation (Current):**
- **Decoupled Prompting:** The language detection feature (`doc.grammar_proofreader_detect_language`) uses an isolated prompt (`LANGUAGE_DETECT_SYSTEM_PROMPT`) *before* any grammar checking occurs. This ensures the model isn't confused by dual instructions.
- **Incomplete Sentence Guard:** If the user hasn't finished typing the sentence (`looks_complete_sentence` fails), language detection and grammar checking are bypassed entirely to prevent jumping the gun on a half-written word.
- **Boundary Preservation:** The pipeline respects LibreOffice's paragraph segmentation constraints (`n_suggested_behind_end`). By returning control when a language changes, we allow LibreOffice to naturally iterate over the newly-detected language chunks.
- **LRU Deduplication:** To prevent redundant LLM calls when a paragraph bounces between locale invalidation updates, we maintain an in-memory `OrderedDict` (`_lang_detect_cache`) bounded to 1,000 sentences. If the text string was recently evaluated for a language, the LLM request is skipped.

**Future Ideas & Cache Alternatives:**
- **Character-Level Heuristics:** Before calling the LLM, we could run a fast regex-based detector for non-Latin scripts (e.g., Japanese, Korean, Arabic). If detected, we can bypass the LLM and instantly apply the matching locale if the document supports it.
- **Persistent Language Cache:** The current LRU is transient (in-memory). We could persist the language map to the `WriterAgentGrammarCache` user-defined property. However, since the primary use case is immediate feedback during typing sessions, the transient in-memory cache is highly effective without bloating the `.odt` file.
- **Model Downgrading:** Language detection requires virtually zero "reasoning." In the future, we could configure the HTTP request to route detection tasks to a significantly cheaper/faster model (e.g., `gemini-flash-8b`) while keeping the heavy grammar evaluation on the primary intelligent model.

<a id="appendix-g-cross-document-memory-sharing"></a>

### Appendix G: Cross-document Memory Sharing (Hybrid L1/L2 Cache)

**Goal:** Allow grammar results to be shared across documents in memory (to support copy-paste hits) while keeping persistence document-specific and ensuring memory is reclaimed when documents close.

**Current Limitation:** By default, `grammar_proofread_cache.py` bypasses the global `_SENTENCE_CACHE` LRU and talks directly to the document's `DocumentPersistence`. This prevents Doc B from seeing results cached for Doc A, even if the same sentence is copied over.

**Proposed Design (Hybrid Tiered Cache):**

1.  **Tier 1 (Global In-Memory LRU):** Continue using `_SENTENCE_CACHE` (default 2048 entries). This cache is global to the Python process and shared by all documents. It is **transient** (never saved to a global disk DB).
2.  **Tier 2 (Per-Document Persistence):** Continue using `DocumentPersistence`. This is document-specific and persists to the `.odt` file.

**Modified Logic Flow:**

-   **`cache_get_sentence`**:
    1.  **Global Hit?** Check `_SENTENCE_CACHE` first. If found, return results immediately. This handles the copy-paste scenario.
    2.  **Document Hit?** If not in `_SENTENCE_CACHE` and `doc_id` is provided, check `DocumentPersistence`.
    3.  **Promotion:** If found in `DocumentPersistence`, "warm up" the global `_SENTENCE_CACHE` with these results so they are available for other documents.
-   **`cache_put_sentence`**:
    1.  **Global Put:** Always store results in `_SENTENCE_CACHE`.
    2.  **Document Put:** If `doc_id` is provided, also store in `DocumentPersistence`.

**Lifecycle & Accumulation:**
-   **Reclamation:** When a document closes, its `DocumentPersistence` (Tier 2) is cleared and removed from the map. The global `_SENTENCE_CACHE` (Tier 1) remains, but its size is capped (2048 entries), so it won't grow indefinitely even with many documents.
-   **No Global Disk Bloat:** The only thing saved to disk is the document itself. The cross-document sharing happens entirely in the transient Tier 1 memory layer.
-   **Copy-Paste Experience:** User copies a checked paragraph from Doc A to Doc B. Doc B calls `doProofreading`. `cache_get_sentence` hits the global Tier 1 cache. Squiggles appear instantly in Doc B without a new LLM call.

**Implementation Pointers:**
-   Modify `cache_get_sentence` in `grammar_proofread_cache.py` to let it fall through the global cache check first.
-   Modify `cache_put_sentence` to always perform the global `_populate_memory_cache_only` step, then conditionally call `p.put` on the document persistence.

### Comparison of Implementation Alternatives

| Approach | Lifecycle Management | Cross-doc Sharing | Complexity | Notes |
|----------|----------------------|-------------------|------------|-------|
| **Hybrid L1/L2 (Proposed)** | L2 is cleared on `OnUnload`. L1 manages itself via global LRU (2048). | Immediate via L1. | Low | Clean separation. `DocumentPersistence` remains the source of truth for a file's state. |
| **Tagged Global Cache** | Global map entries tagged with `set(doc_ids)`. On close, remove `doc_id` from all tags. | Built-in. | Medium | Requires reverse indices to find which sentences belong to a doc for save/cleanup. |
| **Ref-Counted Global Cache** | Entries dropped when count hits 0. | Built-in. | Low | Simple for memory, but doesn't help with the "Save" operation (which sentences belong to this doc?). |

#### Deep Dive: Tagged Global Cache

One alternative is to move to a **single global cache** where every entry is tagged with the IDs of documents that "own" or have requested it.

- **Mechanism**: A map of `fingerprint -> {errors, doc_ids: set()}`.
- **Lifecycle**: When a document closes, the system iterates the cache and removes that `doc_id` from all sets. If a set becomes empty, the entry is either deleted or marked as "orphan" for the global LRU to evict.
- **Save Operation**: Requires a "forward index" (`doc_id -> set(fingerprints)`) to efficiently know which sentences to write to the document's user-defined properties on save.

**Pros:**
- **Zero Memory Redundancy**: A sentence is stored exactly once in memory regardless of how many documents use it.
- **Strict Reclamation**: Can precisely drop sentences that are no longer in use by any open document.

**Cons:**
- **Higher Implementation Surface**: Managing double-indices (fingerprint-to-docs and doc-to-fingerprints) adds complexity and potential for sync bugs.
- **Scanning Overhead**: Scans on document close/save scale with the size of the global cache (though negligible at `MAX_CACHE_SIZE=2048`).

**Conclusion:** The **Hybrid L1/L2** approach is preferred because it leverages existing `DocumentPersistence` logic. It treats the document state as primary and the global memory as a "volatile optimizer." The memory redundancy (storing a sentence in both the global LRU and the document's active map) is minimal (a few hundred KB) compared to the architectural simplicity.

---

## Technical Debt Reduction Plan (Safe, Incremental Cleanup)

**Goal**: Reduce long-term maintenance burden, improve testability and reasoning, and lower the risk of future subtle bugs — **without changing observable behavior** for users or breaking existing tests.

### Guiding Principles for Grammar Debt Work

- **Characterization first**: Before any behavioral change, add or strengthen tests that would fail if the refactoring regressed the current (correct) behavior.
- **Small, reviewable steps**: Prefer many tiny PRs over large ones. Each step must pass the full grammar test suite (`python -m plugin.testing_runner` for native + relevant pytest files).
- **No user-visible changes**: Squiggle behavior, cache semantics, persistence format, performance characteristics, and error messages must remain identical unless a bug is being fixed as part of the cleanup.

### Prioritized Technical Debt Items (TDx)

Below is the list of open technical debt items. All major foundational TD tasks (including bootstrap centralization, testability seams, registry singleton migration, queue simplification, and listener base classes) have been successfully **completed** and shipped.

Furthermore, **the FSM abstraction has been entirely removed** (previously TD11). Because the worker phases are fundamentally sequential (optional language detection followed by grammar checking), the generic FSM loop, transition tables, dispatcher, and state objects were deemed unnecessarily complex. They were replaced by simple sequential `if` checks and direct function calls driven by pure phase decisions in `grammar_worker_phases.py`.

| ID  | Area | Debt Description | Primary Files | Risk | Payoff | Status / Suggested Approach |
|-----|------|------------------|---------------|------|--------|-----------------------------|
| **TD5** | Error handling fragility | Nested try/except "log and continue" patterns in the hot `doProofreading` path and worker (C1). Some errors are swallowed that should at least increment a diagnostic counter. | `ai_grammar_proofreader.py`, `grammar_work_queue.py`, `grammar_proofread_locale.py` | Low | Medium | Implement the tiered error handling table from Appendix B. Introduce a small set of `_safe_*` helpers with clear contracts. |
| **TD6** | Constant & magic number sprawl | Magic numbers and thresholds are defined in multiple files with varying documentation quality (C12). | `grammar_proofread_locale.py`, `grammar_proofread_text.py`, `grammar_work_queue.py`, `grammar_proofread_cache.py` | Low | Medium | Centralize in one well-documented module (or a `GRAMMAR_CONSTANTS` dataclass + docstring). |
| **TD8** | Small helper refactoring & import graph cohesion | Relocate small helper utilities to appropriate files to minimize cross-imports and establish clear conceptual boundaries. | `grammar_proofread_locale.py`, `grammar_proofread_cache.py`, `grammar_proofread_text.py` | Low | Medium | **Open** — Relocate candidates identified in the opportunities subsection below to their natural home modules. |
| **TD9** | Observability & diagnostics debt | `grammar_obs` is useful but under-used in some paths. Batch stats, supersede counts, and LLM durations are only partially instrumented (see C10). | `grammar_obs.py`, `grammar_work_queue.py`, `grammar_worker_llm.py`, `grammar_proofread_cache.py`, `ai_grammar_proofreader.py` | Low | Medium | **Partial (2026-05)** — Canonical `grammar_obs` is active. Remaining: full C10 batch counters / p50–p95 logging. |
| **TD10** | Dead / legacy config surface | References to removed keys (e.g. `doc.grammar_proofreader_wait_timeout_ms`) still linger in some places (C13). | Config schemas, UI bindings, any remaining call sites | Very Low | Low | Mechanical removal pass + test that the keys no longer appear in generated settings. |

### Identified Opportunities – Small Pure Helpers (TD8 Relocation Candidates)

These pure helpers are prime candidates for relocation to improve conceptual ownership and streamline the import graph:

1. **`grammar_inflight_key`** (currently in `grammar_proofread_text.py`):
   - Generates the stable dedup/supersede key (`{doc_id}|{locale}|{hash or INCOMPLETE sentinel}`).
   - Used by: `ai_grammar_proofreader.py` (enqueuing) and `grammar_work_queue.py` (multiple places, including language change requeues).
   - **Suggested new home:** `grammar_proofread_locale.py` (fits naturally with other locale-aware identity logic) or a central identity module.
   
2. **`normalize_reason`** (currently in `grammar_proofread_cache.py`):
   - Canonicalizes error reasons for ignore-rule matching.
   - Used heavily by: `ai_grammar_proofreader.py` (in `ignoreRule`, cached error filtering) and `grammar_work_queue.py` (when processing ignored rules).
   - **Suggested new home:** `grammar_proofread_locale.py` or a dedicated `grammar_rules.py`.

3. **`slice_preview_debug`** (currently in `grammar_obs.py`):
   - Creates compact text previews for logging/observability.
   - Used by: `ai_grammar_proofreader.py` (exposed via the TD2 testing seam) and internally in `grammar_work_queue.py`.
   - **Suggested new home:** `grammar_proofread_text.py` (alongside other text/slice utilities).

### Verification Requirements (Mandatory)

For any TD item that touches runtime behavior:
1. All existing grammar tests (pytest under `tests/writer/locale/` + native tests via `testing_runner`) must pass before and after.
2. Add at least one new regression-style test that would have caught the class of bug being cleaned up.
3. If the change affects persistence or cache format, verify round-trips with old documents.
4. Update this plan document with "Completed" status.

---

**Status of this plan**: Shipped & Current — All major pipeline simplifications (including the removal of FSM state indirection) have been completed.

---

## Future Architecture: Tighter Coupling to the Agent Platform (2026+)

These are longer-horizon ideas that treat the grammar checker less as an independent "linguistic service" and more as a specialized, always-on consumer of the rest of the WriterAgent machinery (cancellation/prioritization, memory, style guidance, document research, and the main agent loop). They accept higher complexity in exchange for consistency and leverage.

### G1. First-class SendCancellation / Priority participation

**Problem today:** Grammar work runs on its own daemon thread + queue with a simple "pause during active agent session" heuristic. Heavy scrolling or background checks can still contend with an active chat/research session for the LLM (even with `llm_request_lane`).

**Idea:** Make `GrammarWorkItem` and the work queue participate in the `SendCancellation` / `agent_session` system more deeply.

- When a main-chat send is active, the grammar worker can be told to de-prioritize (or temporarily suspend) work for the *current foreground document*.
- Use the same `SendCancellation` scope (or a lightweight derived "background priority token") so that a user hitting Stop in chat also drains/supersedes in-flight grammar requests for that document.
- This reuses the existing registration + hook machinery in `queue_executor.py` instead of growing a second cancellation system.

**Benefits:** Consistent "user is busy, back off" behavior across chat and grammar. Fewer surprising LLM calls when the user is in the middle of a big research task.

**Cost / Risk:** Adds another cross-cutting concern to the already subtle cancellation paths. Needs careful testing that grammar still makes forward progress when no chat is active.

**Related existing work:** `SendCancellation`, `agent_session`, `bind_send_stop_checker`, the pause-during-agent flag, and `llm_request_lane`.

### G2. Structural / document-context hints to the LLM

**Current state:** The LLM for grammar sees only the raw sentence text + locale (plus a few system instructions).

**Proposal:** On enqueue, attach a small amount of structural context derived from the LO-DOM / document tree:

- Is this sentence inside a heading (and at what level)?
- Table cell? (row/col headers if available)
- Footnote / endnote / comment?
- List item / caption / frame text?
- Tracked change deletion vs. insertion?

Feed this as a compact prefix or structured field in the grammar prompt (e.g. "Context: Heading 2 | Table cell under 'Revenue' column").

**Rationale:** Many grammar and style rules are highly context-dependent. Models are already good at this when given the hint; we just don't give it today. This is cheap relative to the value and reuses the same `get_document_tree` / proximity machinery the main agent uses.

**Implementation notes:**
- Do the structural lookup in the main thread during `doProofreading` (or lazily in the work item) and attach it to `GrammarWorkItem`.
- Keep the hint small and stable for cache keys (or normalize it away for caching so a sentence moving from body text to a table doesn't create a duplicate cache entry).
- This is a natural consumer of the LO-DOM work.

### G3. Consume agent memory + house style for grammar suggestions

**Idea:** Let the grammar proofreader read from the same memory / style sources the main agent does (`USER.md`, persistent memory entries tagged as "style", `additional_instructions`, or a new "grammar_style" bucket).

Examples of leverage:
- Preferred terminology ("use 'sign in' not 'log in' in this document")
- Tone ("keep suggestions concise and direct; this is technical documentation")
- Domain conventions the user has taught the agent

When the LLM is called for grammar, inject a compact "House style notes for this document" section (similar to how the main chat injects memory).

**Benefits:** Grammar stops feeling like a generic external checker and starts feeling like "the same AI that knows my document and preferences." This is a big part of making the whole product feel coherent rather than a collection of features.

**Trade-offs:** 
- Cache keys become (sentence + active style snapshot). This increases cache invalidation surface.
- Need a clean way to say "these style notes are grammar-relevant" vs. full agent memory.
- Privacy / persistence: style notes that affect squiggles should probably live in the same document-embedded storage as the grammar cache itself.

**Related:** `plugin/chatbot/memory.py`, the librarian mode, `additional_instructions` config key, and how the main tool loop injects context.

### G4. Confidence tiers and "review vs. mechanical" underlining

**Current behavior:** All LLM-flagged issues get the same `TextMarkupType.PROOFREADING` underline. The user has no visual signal of how certain the model was.

**Proposal:** Extend `SingleProofreadingError` payloads (and the cache format) with a simple `confidence` or `tier` field returned by the grammar LLM:

- `mechanical` / high-confidence (spelling patterns, obvious agreement, punctuation the model is very sure about)
- `style` / medium (voice, concision, preferred phrasing)
- `suggestion` / lower (the model thinks this could be improved but is hedging)

Map these to different underline colors or dash styles if UNO allows, or at minimum expose them in the grammar dialog / tooltip so advanced users can filter.

This also lets power users configure "only show mechanical errors from the AI proofreader; send style suggestions to the sidebar chat instead."

**Why now:** The model is already capable of this distinction when asked. The infrastructure (JSON round-tripping, cache, persistence) already carries arbitrary per-error metadata. The missing piece is prompting + a small schema extension.

This turns the grammar checker from a binary "error or not" into something that can participate in the broader "agent does the obvious stuff, human + chat does the judgment calls" workflow.

---

**When to pursue these:** After the current backlog items around cache size, batch diagnostics, and quote-aware splitting are under control. These directions increase coupling between grammar and the rest of the platform; they are most valuable once the grammar engine itself feels solid and low-drama.
