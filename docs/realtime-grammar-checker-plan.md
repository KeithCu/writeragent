# Real-time / AI grammar checking — plan and status

**Status**: Shipped — UNO proofreader + engine + Linguistic `GrammarCheckers` XCU are bundled; batching (paragraph-at-a-time) is enabled and configurable.  
**Authors**: WriterAgent Team  
**Audience**: Developers and PMs aligning on **native Writer linguistic grammar** vs optional **sidebar chat** (different surfaces, different jobs).

### How to use this document

| Section | Use it when you need… |
|--------|------------------------|
| **Concepts and behavior** | UNO proofreader API basics, product boundaries, sentence vs paragraph scheduling |
| **Shipped implementation reference** | Module map, settings keys, runtime/cache/queue behavior, tests |
| **Open backlog** | Remaining work items |
| **Appendices** | [Dialogue / BreakIterator split limitation](#appendix-e-dialogue-splits) |

### At a glance

- **Native grammar** is implemented as an `XProofreader` service with Lightproof-style registry (`LinguisticWriterAgentGrammar.xcu`); users enable LLM work on the **Doc** tab and pick the proofreader under Writing aids.
- **Batching** groups sentences from the same paragraph into chunked LLM requests; batch size is capped (`doc.grammar_proofreader_batch_sentences`, max 8).
- **Concurrent requests** (`doc.grammar_proofreader_max_in_flight`, 1–8, default **1**): up to N background drain workers and matching grammar HTTP slots; **1** keeps prior global `llm_request_lane` behavior with chat; **>1** allows parallel grammar API calls (e.g. OpenRouter).
- **Language Detection** (`doc.grammar_proofreader_detect_language`: **Off** / **AI (LLM)** / **Local (langdetect)**) compares sentence language to document `CharLocale`. LLM mode uses a lightweight API call; Local mode uses PyPI `langdetect` in the **embeddings venv worker** ([`langdetect_service.py`](../plugin/framework/client/langdetect_service.py) → [`langdetect_rpc.py`](../plugin/embeddings/venv/langdetect_rpc.py); requires Settings → Python venv). Mismatches trigger locale update and re-check.
- **Cache** is **document-embedded** (`.odt` user property `WriterAgentGrammarCache`) plus a **global in-memory LRU** (2048 entries) shared across open documents for copy-paste hits. The old profile SQLite cache was removed.
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

### Runtime behavior

#### Foreground path (`doProofreading`) and UI hooks

- **`doProofreading`** (async return path): On a **full cache miss**, WriterAgent returns with empty `aErrors` and enqueues a work item. On a **partial cache hit** (some sentences cached, some not), it **returns the cached errors immediately** (better than empty — squiggles appear for already-checked sentences) and enqueues for the remaining uncached sentences. On a **full cache hit** all errors are returned directly, no enqueue needed. It **does not** wait inside `doProofreading` or pump `processEventsToIdle()` for results. That keeps **menus and chrome responsive** while grammar runs.
- **Sidebar status**: the proofreader emits `grammar:status` for meaningful phases (`start`, `request`, `complete`, `failed`, etc.). Skipped work is not reported to the status bar.
- **Async delivery watchdog  (In GitHub branch only)**: when the worker caches **non-empty** errors, [`grammar_delivery.py`](../plugin/writer/locale/grammar_delivery.py) marks the sentence pending until LO calls `doProofreading` again and reads the warm cache. If LO has not retrieved the result within **5 seconds**, a coalesced main-thread nudge runs (at most **2** attempts per sentence, **5 s** cooldown per document): first `PROOFREAD_AGAIN` via `XLinguServiceEventBroadcaster` on the live proofreader instance, then targeted `XProofreadingIterator.checkSentenceAtPosition` for pending sentences only, then a weak window `invalidate(0)` repaint. Clean (zero-issue) cache rows do not schedule nudges.

#### Worker thread, quiet period, and batching

- **Concurrency / work queue**: Up to **`doc.grammar_proofreader_max_in_flight`** persistent daemon drain threads (default **1**, max **8**) share one `GrammarWorkQueue` (`grammar_work_queue.py`). Each thread **batch-drains** pending items, deduplicates them, and **groups by (document, locale)**. Grammar HTTP uses [`grammar_llm_request_gate()`](../plugin/framework/queue_executor.py): limit **1** → global `llm_request_lane()` (serialized with chat); limit **>1** → up to N concurrent grammar/lang-detect calls (chat still uses its own lane).
- **Quiet period**: The worker uses `queue.Queue.get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)` (see `grammar_proofread_locale.py`) so batches wait for a short idle window rather than spamming the LLM on every micro-edit.
- **Paragraph batching (chunked)**: Grouped sentences from the same paragraph/context are sent to the LLM in batches. Chunks respect **`doc.grammar_proofreader_batch_sentences`** (hard-capped by **`GRAMMAR_BATCH_MAX_SENTENCES = 8`**). This reduces latency and token overhead during full-paragraph checks or document loads when set to values greater than 1.
- **Empty LLM content (single sentence)**: Some reasoning models (e.g. Inception Mercury via OpenRouter) may return `content: null` with `finish_reason: stop` instead of `{"errors": []}`. Grammar does **not** retry those calls; a single-sentence empty body is treated as **no issues**, cached as a clean row, and logged at DEBUG only. Batch mode still treats a wholly empty/unparseable response as a failure (no cache). Tradeoff: a true provider outage on one sentence could skip flagging until the text changes and invalidates cache.

<a id="language-detection-shipped"></a>

#### Language detection (shipped)

Settings key **`doc.grammar_proofreader_detect_language`** (Doc tab: **Off** / **AI (LLM)** / **Local (langdetect)**). When not **Off**, the worker compares each complete sentence’s detected language to the document `CharLocale`; on mismatch it updates paragraph locale and re-queues grammar in the detected language ([`detect_languages_for_chunk`](../plugin/writer/locale/grammar_worker_llm.py), [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)).

| Mode | Implementation |
|------|----------------|
| **Off** | No detection; grammar uses document locale only. |
| **AI (LLM)** | Batch or single-sentence API call via `language_detect_llm_sync`; shares `grammar_llm_request_gate` with grammar HTTP. |
| **Local (langdetect)** | PyPI `langdetect` in the embeddings venv worker (`_detect_languages_via_langdetect` → [`langdetect_service.detect_languages`](../plugin/framework/client/langdetect_service.py)); requires configured venv + `langdetect` in [`EMBEDDINGS_VENV_PIP_INSTALL`](../plugin/embeddings/venv/embeddings_index.py). Missing venv/package fails the detection pass (Harper-style). |

Shared behavior for **LLM** and **langdetect** modes: in-memory language LRU (`get_cached_language` / `put_cached_language`); skip detection for incomplete sentences; optional persisted-grammar heuristic to avoid re-detecting known-good text; `normalize_detected_bcp47` maps detector output to grammar-registry tags. Regression: [`test_grammar_worker_llm.py`](../tests/writer/locale/test_grammar_worker_llm.py), [`test_langdetect_profiles.py`](../tests/writer/locale/test_langdetect_profiles.py).

#### Tail enqueue, dedup keys, stale detection, diagnostics

- **Same-key newest wins + stale suppression**: Drain-time **`deduplicate_grammar_batch`** keeps, for each **`inflight_key`**, only the item with the highest **`enqueue_seq`** (Layer 2 drain dict + Layer 3 `_latest_seq` guards; see [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py) module docstring).
- **`inflight_key` logic (stable key)**:
    - **Complete**: `{doc_id}|{locale}|{hash(sent_text)}`. Ensures uniqueness across paragraphs and stability if the sentence is not being edited.
    - **Incomplete**: `{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING`. Ensures all partial drafts for the active typing spot supersede each other, preventing typing floods.
- **Stale guard**: `GrammarWorkQueue` performs a **pre-execute stale check** against `_latest_seq` and skips any survivor older than the latest known sequence for that key. After each LLM response returns, **`cache_put_sentence` is skipped** if a newer enqueue superseded this item during the HTTP call (`inflight_superseded`).
- **Queue diagnostics**: DEBUG `grammar_obs` events (`queue_enqueue`, `queue_drain_batch`, `batch_stats` with `sentences_queued` / `sentences_deduped` / `sentences_stale_skipped` / `sentences_llm_requested` / `llm_request_duration_ms`). Out-of-order `enqueue_seq` logs at ERROR.

#### Sentence gating and pinned text

- **Sentence-level gating**: grammar checks run when the slice looks like a complete sentence (terminal punctuation heuristic with multilingual marks such as `. ! ? … ؟ 。 ！ ？ ।`) **or** when partial text reaches `GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS` (15 non-space chars). Short incomplete fragments are skipped before cache/worker scheduling.
- **Pinned sentence text on enqueue**: Each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) carries **`text`** — the exact sentence segment chosen during `doProofreading`. The worker uses it for LLM + cache and **does not** call `split_into_sentences` again on the slice, avoiding BreakIterator disagreements between substring vs full-buffer splits.

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

- **In-memory LRU (L1)**: Global `grammar_registry.sentence_cache`, keyed by locale + sentence fingerprint. `MAX_CACHE_SIZE` is **2048**. Shared across open documents in-session ([`test_cross_document_l1_cache_hit`](../tests/writer/locale/test_grammar_proofread_cache.py)).
- **Document persistence (L2)**: Per-`.odt` user property `WriterAgentGrammarCache` via [`DocumentPersistence`](../plugin/writer/locale/grammar_persistence.py). `cache_get_sentence` checks L1, then L2, and promotes L2 hits into L1. `cache_put_sentence` always writes L1; writes L2 when `doc_id` is set.
- **Normalization**: Uses `_normalize_for_sentence_cache` so trailing whitespace and redundant punctuation share keys. Errors are clipped to the canonical length.
- **Incomplete-prefix compaction**: On **`cache_put_sentence`**, when the normalized text is still **incomplete**, the cache walks the sentence `OrderedDict` newest-first (bounded scan per locale) and evicts strict-prefix incomplete predecessors so incremental typing does not fill the LRU with `"The"`, `"The qu"`, … stubs. Details and regression tests: [`grammar_proofread_cache.py`](../plugin/writer/locale/grammar_proofread_cache.py), [`test_sentence_cache_incomplete_prefix_compaction`](../tests/writer/locale/test_grammar_proofread_cache.py). Document-embedded mode (`doc_id` set) skips this compaction scan but still warms the global LRU.
- **Memory warm-up**: Persistence hits promoted to L1 via `_populate_memory_cache_only`.
- **Document-embedded persistence**: Loads on first grammar call; saves on **`OnPrepareSave`** / **`OnSave`** / **`OnSaveAs`** / **`OnSaveTo`** via `set_document_property` in [`plugin/doc/document_helpers.py`](../plugin/doc/document_helpers.py). Registry cleanup on `OnUnload` / dispose.

> [!NOTE]
> <a id="document-embedded-cache-default"></a>
> **Document-embedded cache (v2)**
>
> Grammar results travel with the `.odt` as `WriterAgentGrammarCache` (v2: concatenated `good` fingerprints + `bad` error map, 24-hex keys, compact error fields). Serialized JSON capped at **900 KB**; over that, save is skipped with a warning.
>
> **Trade-offs:** Cross-file reuse is session-only via L1 (not on disk). Shared `.odt` files carry sentence fingerprints and error payloads — strip user-defined properties before sharing sensitive drafts. Save writes only `_session_accessed` sentences (see backlog **P22**). Regression: [`test_grammar_persistence.py`](../tests/writer/locale/test_grammar_persistence.py).


#### LLM wire format and parser

- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/framework/client/llm_client.py) with `response_format={"type":"json_object"}` on the OpenAI-compatible path (Together, OpenRouter, etc.; see docstring on `make_chat_request`), a system prompt (**`GRAMMAR_SYSTEM_PROMPT_TEMPLATE`** in [`grammar_proofread_locale.py`](../plugin/writer/locale/grammar_proofread_locale.py)) requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}` (schema description in English) plus the **document language** (BCP-47 and English name from the registry), and user message the **checked sentence text** for that worker item (one sentence per request in normal prose). The prompt explicitly asks for errors in the order they appear. For threshold-allowed partial slices, the prompt adds a conservative note that input may be partial. Parser: [`parse_grammar_json`](../plugin/writer/locale/grammar_proofread_json.py) uses `safe_json_loads` then `json_repair` (with logging) when needed.

#### Offsets, whitespace, and markup

- **Offset normalization**: `normalize_errors_for_text` uses **`search_pos` tracking** to handle multiple occurrences of the same erroneous text within a window. If ordered scan fails and a global `find` matches **before** `search_pos`, that item is **skipped** (avoids anchoring duplicate substrings to the wrong occurrence).
- **Traversal whitespace**: `_apply_proofreading_end_positions` and initial empty-result advancement use Unicode **`str.isspace()`**, not ASCII space only, so tabs/NBSP between sentences advance Writer's next position correctly.
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### Why `enqueue_seq` exists (queue FIFO is not enough)

Global monotonic counter (`next_enqueue_seq()` in [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)); each [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) stores **`enqueue_seq`** as a generation stamp for supersede/dedup — not a queue position. Used by: batch drain + `deduplicate_grammar_batch` (highest seq per `inflight_key`), pre-execute stale skip, and post-LLM `inflight_superseded` before `cache_put_sentence`.

### Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on the **Doc** tab; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | `doProofreading` does **not** wait on the main thread for LLM results (avoids dead menus while grammar runs). HTTP/LLM runs on a background worker; underlines update on a **later** proofreading pass when the sentence cache is ready. |
| Stale underlines | Sentence cache (locale + sentence text fingerprint) plus work queue with same-key supersede, pre-execute stale skip, and post-LLM cache-write guard. **Cache hit** → immediate errors; **miss** → empty return once, queue workers fill cache for the next pass. See **Open backlog** for evolving this. |
| Concurrent chat agent | Optional guard (`doc.grammar_proofreader_pause_during_agent`) skips grammar while chat/agent runs. With **`doc.grammar_proofreader_max_in_flight` = 1**, grammar HTTP shares global `llm_request_lane` with chat. With **>1**, grammar requests can overlap chat unless pause is enabled—raise concurrency only when the endpoint tolerates it. |

---

## Open backlog

Two tables: **product / hardening** (user-visible or systemic improvements) and **code health** (maintainability). Status is **open** unless noted.

### Product and hardening

| ID | Task | Notes |
|----|------|--------|
| P2 | HTTP 429 / backoff | Theoretical: Exponential backoff and cooldown in the grammar worker if providers ever rate-limit; currently unnecessary due to `LlmClient` request pacing. |
| P3 | Locales | Optional regional tags in XCU if an LO build needs explicit `hasLocale`/`getLocales` pairing beyond normalization. |
| P4 | Refresh UX | **Shipped (partial):** delivery tracker + 5 s watchdog nudge in [`grammar_delivery.py`](../plugin/writer/locale/grammar_delivery.py). Remaining: verify LO listener wiring across builds; optional user-facing doc note that squiggles may lag briefly while typing stops. |
| P6 | Document-generation invalidation | Fold revision/mod-generation into cache keys if LO exposes it; reduces stale offsets after edits above span. |
| P7 | Shared policy with chat | Expand beyond pause-during-agent + shared LLM lane (endpoint-aware policy, status UX, adaptive queue). |
| P8 | Prompt and schema hardening | Few-shot edge cases (quotes, lists, track changes); stricter JSON recovery. |
| P11 | Observability | Cache hit rate, supersede counts, p50/p95 schedule→`cache_put` behind a verbose flag. |
| P13 | LanguageTool-class local checking | Research roadmap: [docs/languagetool-local-parity-phased-plan.md](languagetool-local-parity-phased-plan.md). |
| P14 | Parallel grammar worker | Shrink extra workers when user lowers `doc.grammar_proofreader_max_in_flight` without restart; optional distinct-document-only scheduling. |
| P15 | Queue priority / visibility | Prefer currently edited or visible ranges over scroll-induced backlog (see **C5**). |
| P17 | Configurable LLM max tokens | Expose the hardcoded **3072** max output tokens as `doc.grammar_proofreader_max_tokens` so users can tune for different endpoints or models. |
| P18 | Configurable max chars | Move `GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS` (8192) to a config key `doc.grammar_proofreader_max_chars`; allows tuning for very long sentences without code changes. |
| P19 | Batch size validation | Enforce `1 <= doc.grammar_proofreader_batch_sentences <= 8` at config read time; log **WARNING** if out of range and clamp to bounds. |
| P22 | Embedded cache: full-document retention on save | Today `_persist_to_udprops` only writes sentences in `_session_accessed` (touched this session). Document sections that were never scrolled into view can drop after a save. Either persist `_memory_cache` in full when small enough, or always re-include previously persisted fingerprints we haven't explicitly invalidated. Trade-off vs cap and edit-detection cost. |
| P24 | Regional locale opt-out | Allow specific locales (e.g., `en-AU`, `pt-PT`) to opt-out of normalization to the "base" language if regional grammar nuances are significant. |
| P25 | Quote-aware sentence merge (optional) | Reduce false "missing quote" on dialogue split at `!`/`?` inside quotes. Requires capped post-`split_into_sentences` merge, i18n-safe quote rules, UNO + unit tests. See [Dialogue / BreakIterator limitation](#dialogue-breakiterator-limitation) and [Appendix E](#appendix-e-dialogue-splits). |

### Code health and maintainability

| ID | Task | Notes |
|----|------|--------|
| C1 | Tiered error handling in `doProofreading` / worker | Reduce nested try/except that only log-and-continue; extract `_safe_*` helpers per [Appendix B](#appendix-b-debt-work-notes). Characterization tests first; no user-visible behavior change. |
| C2 | Optional `unohelper` consolidation | Top-level import serves `unohelper.Base`; registration block imports again for `ImplementationHelper` — optional single pattern for clarity. |
| C5 | Queue priority / visibility heuristic | Prefer currently edited or visible ranges over scroll-induced backlog when draining the grammar queue. Product-facing UX in **P15**; no implementation yet. |
| C6 | Regex audit | Most patterns are compiled; audit [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py) for any remaining compile-per-call hot paths. |
| C7 | Logging discipline | Structured events, avoid duplicate levels, DEBUG vs INFO boundaries. |
| C8 | ProofreadingResult helpers / hints | Optional `@dataclass`-style helpers or richer type hints for UNO structs where stubs help. |

---

## Appendices

<a id="appendix-b-debt-work-notes"></a>
### Appendix B: Debt-work notes

**Error handling tiers** (for **C1**):

| Level | Action | Example |
|-------|--------|---------|
| **Fatal** | Raise / return None | UNO module missing, `createUnoStruct` fails |
| **Recoverable** | Log ERROR + return empty/default | Config read fails, locale not supported |
| **Diagnostic** | Log INFO/DEBUG + continue | Cache miss, queue deduplication |

**Refactor rules:** characterization tests before behavior-touching cleanup; small PRs; full `tests/writer/locale/` pytest + UNO grammar tests must pass; persistence round-trip if cache format changes.

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
4. **Pinned text to the LLM:** [`WriterAgentAiGrammarProofreader`](../plugin/writer/locale/ai_grammar_proofreader.py) enqueues [`GrammarWorkItem`](../plugin/writer/locale/grammar_work_queue.py) with **`text`** set to that segment. The worker does **not** re-split, so the model genuinely receives a **truncated quoted string**.

**Why this is a hardening problem, not “fix the prompt only”:** Prompt tweaks might reduce false positives but do not fix **wrong work units**, **cache granularity**, or **offset** semantics if we ever map errors back across merged spans.

**Robust mitigation ideas (design space):**

| Direction | Strength | Risk |
|-----------|----------|------|
| **Post-split merge with quote parity** | Aligns LLM input with author intent for many dialogue lines | Apostrophes, nested `"`/`'`, guillemets, RTL, and mixed curly/ASCII quotes need explicit policy; wrong parity can merge too much or too little. |
| **Hard cap on forward merge** | Prevents one open quote from absorbing **many** BreakIterator sentences until the closing quote—avoiding runaway **token cost**, **cache key bloat**, and **8192-char** pressure | Below the cap, some long speech blocks may still split incorrectly; above the cap, fall back must be defined. |
| **Trigger only on `!`/`?` boundaries** | Avoids touching the common `.` + abbrev path | Misses edge cases like period-inside-dialogue when BI still splits early. |
| **Prompt-only “ignore incomplete quote”** | Cheap | Leaves bad segmentation and weak cache behavior unchanged. |

**Regression surface:** Any change should add coverage in [`tests/writer/locale/test_grammar_proofread_text_uno.py`](../tests/writer/locale/test_grammar_proofread_text_uno.py) (real `BreakIterator`) and focused unit tests for merge helpers without UNO.

**Status:** Documented limitation + backlog **P25**; no code change required for users who hit this rarely.

### Appendix F: Language detection — optional enhancements

**Local (langdetect)** and **AI (LLM)** detection are shipped (see [Language detection (shipped)](#language-detection-shipped) and **At a glance**). Possible follow-ups:

- **Character-level heuristics:** Fast script detection before LLM or langdetect (Japanese, Korean, Arabic, …).
- **Persistent language cache:** Persist lang map in `WriterAgentGrammarCache` (trade-off vs `.odt` size).
- **Model downgrading:** Route **LLM** detection to a cheaper/faster model than grammar evaluation (langdetect mode already avoids API cost).

---

## Future Architecture: Tighter Coupling to the Agent Platform (2026+)

These are longer-horizon ideas that treat the grammar checker less as an independent "linguistic service" and more as a specialized, always-on consumer of the rest of the WriterAgent machinery (cancellation/prioritization, memory, style guidance, document research, and the main agent loop). They accept higher complexity in exchange for consistency and leverage.


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


---

**When to pursue these:** After **P25** (quote-aware splitting) and **C1** (error-handling cleanup) are under control.
